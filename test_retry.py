import copy
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import oci
from retry import Retry, Stop, SHAPE, E2_SHAPE, NAME, is_capacity_error, launch_details


def capacity():
    return oci.exceptions.ServiceError(500, 'InternalError', {}, 'Out of host capacity.')


class Store:
    def __init__(self, **state):
        self.state = {'phase': 'ready', **state}
        self.history = []
        self.notices = []
        self.created_notices = []

    def save(self, **values):
        self.state.update(values)
        self.history.append(copy.deepcopy(self.state))

    def notify_created(self, message):
        if not self.state.get("created_notified"):
            self.created_notices.append(message)
            self.save(created_notified=True)

    def notify_and_stop(self, message):
        self.save(phase='stopped')
        self.notices.append(message)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.sleeper = patch("retry.time.sleep").start()
        self.addCleanup(patch.stopall)

    def make_retry(self, **state):
        r = Retry.__new__(Retry)
        r.store = Store(**state)
        r.compartment, r.subnet, r.image, r.public_key = 'c', 's', 'i', 'ssh-ed25519 AAAATEST'
        r.compute, r.network = Mock(), Mock()
        r.inventory = Mock(return_value=([NS(name=f'AD-{x}') for x in (1, 2, 3)], [], 0))
        r.preflight = Mock()
        r.compute.list_shapes.return_value = oci.response.Response(200, {}, [oci.core.models.Shape(
            shape=SHAPE, billing_type='LIMITED_FREE', ocpu_options=oci.core.models.ShapeOcpuOptions(min=1),
            memory_options=oci.core.models.ShapeMemoryOptions(min_in_g_bs=1, min_per_ocpu_in_gbs=1))], None)
        return r

    def test_inventory_uses_sdk_keyword_only_volume_queries(self):
        r = Retry.__new__(Retry)
        r.config = {'tenancy': 't'}
        r.compartment = 't'
        r.identity, r.compute, r.block = Mock(), Mock(), Mock()
        r.identity.list_region_subscriptions.return_value.data = [NS(region_name='us-chicago-1', is_home_region=True)]
        r.identity.list_compartments.return_value = oci.response.Response(200, {}, [], None)
        r.identity.list_availability_domains.return_value.data = [NS(name='ad')]
        r.compute.list_instances.return_value = oci.response.Response(200, {}, [], None)
        def volumes(*, compartment_id):
            return oci.response.Response(200, {}, [NS(size_in_gbs=40, lifecycle_state='AVAILABLE')], None)
        def boot_volumes(*, availability_domain, compartment_id):
            return oci.response.Response(200, {}, [NS(size_in_gbs=50, lifecycle_state='AVAILABLE')], None)
        r.block.list_volumes.side_effect = volumes
        r.block.list_boot_volumes.side_effect = boot_volumes
        self.assertEqual(r.inventory()[2], 90)

    def test_non_free_shape_billing_blocks_launch(self):
        r = self.make_retry()
        r.compute.list_shapes.return_value.data[0].billing_type = 'PAID'
        with patch.dict(os.environ, RETRY_ENABLED='true'), self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_fixed_request_never_uses_paid_shape_or_larger_config(self):
        d = launch_details('c', 's', 'i', 'ssh-ed25519 test', 'ad')
        self.assertEqual((d.shape, d.shape_config.ocpus, d.shape_config.memory_in_gbs), (SHAPE, 1, 6))
        self.assertEqual((d.source_details.boot_volume_size_in_gbs, d.source_details.boot_volume_vpus_per_gb), (50, 10))
        self.assertTrue(d.create_vnic_details.assign_public_ip)
        self.assertIsNone(d.capacity_reservation_id)
        self.assertIsNone(d.fault_domain)
        self.assertEqual(d.metadata, {'ssh_authorized_keys': 'ssh-ed25519 test'})

    def test_three_explicit_capacity_failures_and_no_notification(self):
        r = self.make_retry()
        r.compute.launch_instance.side_effect = capacity()
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 3)
        self.assertEqual(r.store.state['phase'], 'ready')
        self.assertFalse(r.store.notices)
        self.assertEqual(len({c.kwargs['opc_retry_token'] for c in r.compute.launch_instance.call_args_list}), 3)

    def test_timeout_is_not_capacity_and_prevents_second_launch(self):
        r = self.make_retry()
        def crash(*a, **kw):
            self.assertEqual(r.store.state['phase'], 'launch_pending')
            raise TimeoutError()
        r.compute.launch_instance.side_effect = crash
        with patch.dict(os.environ, RETRY_ENABLED='true'), self.assertRaises(Stop):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 1)
        self.assertEqual(r.store.state['phase'], 'launch_pending')

    def test_previous_ambiguous_request_blocks_new_launch(self):
        r = self.make_retry(phase='launch_pending')
        with self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_read_error_never_becomes_empty_inventory(self):
        r = self.make_retry()
        r.inventory.side_effect = PermissionError()
        with self.assertRaises(PermissionError):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_excess_volume_storage_blocks_launch(self):
        r = self.make_retry()
        r.inventory.return_value = ([], [], 151)
        with self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_other_existing_instance_blocks_launch(self):
        r = self.make_retry()
        r.inventory.return_value = ([], [NS(id='other', display_name='other')], 0)
        with self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_created_instance_stops_even_when_ip_lookup_fails(self):
        r = self.make_retry()
        instance = NS(id='created', display_name=NAME, compartment_id='c', lifecycle_state='PROVISIONING', shape=SHAPE)
        r.compute.launch_instance.return_value = NS(data=instance)
        r.compute.list_vnic_attachments.side_effect = PermissionError()
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 1)
        self.assertEqual(r.store.state['instance_id'], 'created')
        self.assertEqual(r.store.state['phase'], 'stopped')
        self.assertIn('尚待确认', r.store.notices[0])

    def test_preexisting_target_never_creates_another(self):
        r = self.make_retry(phase='launch_pending')
        target = NS(id='existing', display_name=NAME, compartment_id='c', lifecycle_state='RUNNING', shape=SHAPE)
        r.inventory.return_value = ([], [target], 50)
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        r.run('retry')
        r.compute.launch_instance.assert_not_called()
        self.assertEqual(r.store.state['instance_id'], 'existing')

    def test_check_mode_never_launches(self):
        r = self.make_retry()
        r.run('check')
        r.compute.launch_instance.assert_not_called()
        self.assertFalse(r.store.notices)

    def test_disabled_mode_never_launches(self):
        r = self.make_retry()
        with patch.dict(os.environ, RETRY_ENABLED='false'), self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def test_unrelated_api_errors_do_not_trigger_next_ad(self):
        for status, message in ((429, 'Too many requests'), (500, 'Unknown internal error'), (401, 'NotAuthenticated')):
            self.assertFalse(is_capacity_error(oci.exceptions.ServiceError(status, 'Error', {}, message)))
        self.assertTrue(is_capacity_error(capacity()))

    def test_minimum_memory_is_not_silently_increased(self):
        r = self.make_retry()
        r.compute.list_shapes.return_value.data[0].memory_options.min_per_ocpu_in_gbs = 7
        with patch.dict(os.environ, RETRY_ENABLED='true'), self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()

    def with_e2(self, **state):
        r = self.make_retry(**state)
        a1 = r.compute.list_shapes.return_value.data[0]
        e2 = oci.core.models.Shape(shape=E2_SHAPE, billing_type='ALWAYS_FREE',
                                   is_flexible=False, ocpus=1, memory_in_gbs=1)
        def shapes(*args, **kwargs):
            rows = [a1]
            if 'image_id' not in kwargs and kwargs.get('availability_domain') == 'AD-1':
                rows = [a1, e2]
            return oci.response.Response(200, {}, rows, None)
        r.compute.list_shapes.side_effect = shapes
        r.compute.list_images.return_value = oci.response.Response(200, {}, [NS(
            id='x86-image', compartment_id=None, operating_system='Canonical Ubuntu',
            operating_system_version='24.04 Minimal',
            display_name='Canonical-Ubuntu-24.04-Minimal-2026.11.01-0',
            lifecycle_state='AVAILABLE', size_in_mbs=2500)], None)
        r.compute.get_image_shape_compatibility_entry.return_value.data = NS(
            memory_constraints=None, ocpu_constraints=None)
        return r, e2

    def test_e2_success_after_three_a1_capacity_errors_stops(self):
        r, _ = self.with_e2()
        instance = NS(id='e2-created', display_name=NAME, compartment_id='c',
                      lifecycle_state='PROVISIONING', shape=E2_SHAPE)
        r.compute.launch_instance.side_effect = [capacity(), capacity(), capacity(), NS(data=instance)]
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 4)
        d = r.compute.launch_instance.call_args.args[0]
        self.assertEqual((d.shape, d.source_details.image_id), (E2_SHAPE, 'x86-image'))
        self.assertIsNone(d.shape_config)
        self.assertIsNone(d.fault_domain)
        self.assertEqual((d.source_details.boot_volume_size_in_gbs, d.source_details.boot_volume_vpus_per_gb), (50, 10))
        self.assertEqual(r.store.state['instance_id'], 'e2-created')
        self.assertEqual(r.store.state['phase'], 'stopped')
        self.assertEqual(len(r.store.notices), 1)
        pending = [s for s in r.store.history if s['phase'] == 'launch_pending'][-1]
        self.assertEqual((pending['shape'], pending['image_id']), (E2_SHAPE, 'x86-image'))

    def test_e2_is_skipped_when_not_offered_without_querying_x86_image(self):
        r = self.make_retry()
        r.run('check')
        r.compute.list_images.assert_not_called()
        r.compute.launch_instance.assert_not_called()
        self.assertTrue(all('image_id' not in c.kwargs for c in r.compute.list_shapes.call_args_list))

    def test_e2_non_free_billing_blocks_all_launches(self):
        for billing in ('PAID', 'LIMITED_FREE', None):
            r, e2 = self.with_e2()
            e2.billing_type = billing
            with self.assertRaises(Stop):
                r.run('check')
            r.compute.launch_instance.assert_not_called()

    def test_e2_missing_or_wrong_architecture_or_custom_image_is_skipped(self):
        for field, value in (('compartment_id', 'custom'),
                             ('display_name', 'Canonical-Ubuntu-24.04-Minimal-aarch64-2026'),
                             ('operating_system_version', '24.04 Minimal aarch64'),
                             ('size_in_mbs', 60000), ('lifecycle_state', 'DISABLED')):
            r, _ = self.with_e2()
            setattr(r.compute.list_images.return_value.data[0], field, value)
            self.assertIsNone(r.e2_candidate([NS(name='AD-1')]))
        r, _ = self.with_e2()
        r.compute.list_images.return_value.data = []
        self.assertIsNone(r.e2_candidate([NS(name='AD-1')]))

    def test_e2_image_requiring_more_memory_blocks_launch(self):
        r, _ = self.with_e2()
        r.compute.get_image_shape_compatibility_entry.return_value.data.memory_constraints = NS(min_in_gbs=2)
        with self.assertRaises(Stop):
            r.run('check')
        r.compute.launch_instance.assert_not_called()

    def test_e2_ambiguous_request_stays_pending(self):
        r, _ = self.with_e2()
        r.compute.launch_instance.side_effect = [capacity(), capacity(), capacity(), TimeoutError()]
        with patch.dict(os.environ, RETRY_ENABLED='true'), self.assertRaises(Stop):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 4)
        self.assertEqual(r.store.state['phase'], 'launch_pending')
        self.assertEqual(r.store.state['shape'], E2_SHAPE)

    def test_all_capacity_failures_are_bounded_and_quiet(self):
        r, _ = self.with_e2()
        r.compute.launch_instance.side_effect = capacity()
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 4)
        self.assertEqual(r.store.state['phase'], 'ready')
        self.assertFalse(r.store.notices)

    def test_new_target_before_e2_is_followed_not_duplicated(self):
        r, _ = self.with_e2()
        target = NS(id='found', display_name=NAME, compartment_id='c',
                    lifecycle_state='PROVISIONING', shape=SHAPE)
        initial = r.inventory.return_value
        r.inventory.side_effect = [initial, (initial[0], [target], 50)]
        r.compute.launch_instance.side_effect = capacity()
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 3)
        self.assertEqual(r.store.state['instance_id'], 'found')

    def test_disk_residue_before_e2_blocks_launch(self):
        r, _ = self.with_e2()
        initial = r.inventory.return_value
        r.inventory.side_effect = [initial, (initial[0], [], 50)]
        r.compute.launch_instance.side_effect = capacity()
        with patch.dict(os.environ, RETRY_ENABLED='true'), self.assertRaises(Stop):
            r.run('retry')
        self.assertEqual(r.compute.launch_instance.call_count, 3)

    def test_launch_builder_rejects_paid_shape(self):
        with self.assertRaises(Stop):
            launch_details('c', 's', 'i', 'ssh-ed25519 test', 'ad', 'VM.Standard.E5.Flex')

    def a1(self, state='RUNNING', cpu=1, memory=6):
        return NS(id='created', display_name=NAME, compartment_id='c', shape=SHAPE,
                  availability_domain='AD-1', lifecycle_state=state,
                  shape_config=NS(ocpus=cpu, memory_in_gbs=memory))

    def test_provisioning_is_saved_and_notified_without_resize_or_second_launch(self):
        r = self.make_retry()
        r.compute.launch_instance.return_value = NS(data=self.a1('PROVISIONING'))
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        with patch.dict(os.environ, RETRY_ENABLED='true'):
            r.run('retry')
        self.assertEqual(r.store.state['instance_id'], 'created')
        self.assertEqual(r.store.state['phase'], 'created')
        self.assertEqual(r.compute.launch_instance.call_count, 1)
        r.compute.update_instance.assert_not_called()
        self.assertEqual(len(r.store.created_notices), 1)
        self.assertFalse(r.store.notices)

    def test_running_a1_resizes_same_id_once_and_then_waits(self):
        r = self.make_retry(phase='created', instance_id='created')
        r.inventory.return_value = (r.inventory.return_value[0], [self.a1()], 50)
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        def update(i, detail, **kwargs):
            self.assertTrue(r.store.state['resize_attempted'])
            self.assertEqual(i, 'created')
            self.assertEqual((detail.shape_config.ocpus, detail.shape_config.memory_in_gbs), (2, 12))
            self.assertIsNone(detail.shape)
        r.compute.update_instance.side_effect = update
        r.run('retry')
        r.run('retry')
        r.compute.launch_instance.assert_not_called()
        self.assertEqual(r.compute.update_instance.call_count, 1)
        self.assertEqual(len(r.store.created_notices), 1)
        self.assertFalse(r.store.notices)

    def test_successful_resize_notifies_and_stops(self):
        r = self.make_retry(phase='created', instance_id='created', resize_attempted=True)
        r.inventory.return_value = (r.inventory.return_value[0], [self.a1(cpu=2, memory=12)], 50)
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        r.run('retry')
        self.assertEqual(r.store.state['phase'], 'stopped')
        self.assertEqual(r.store.state['resize_status'], 'complete')
        r.compute.launch_instance.assert_not_called()
        r.compute.update_instance.assert_not_called()

    def test_resize_capacity_or_timeout_retains_identity_without_second_attempt(self):
        for error in (capacity(), TimeoutError()):
            r = self.make_retry(phase='created', instance_id='created')
            r.inventory.return_value = (r.inventory.return_value[0], [self.a1()], 50)
            r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
            r.compute.update_instance.side_effect = error
            with self.assertRaises(Stop):
                r.run('retry')
            self.assertEqual(r.store.state['instance_id'], 'created')
            self.assertTrue(r.store.state['resize_attempted'])
            r.run('retry')
            self.assertEqual(r.compute.update_instance.call_count, 1)
            r.compute.launch_instance.assert_not_called()
            r.compute.terminate_instance.assert_not_called()

    def test_resize_stops_when_other_resources_appear_or_shape_not_free(self):
        for cause in ('other_instance', 'paid_shape'):
            r = self.make_retry(phase='created', instance_id='created')
            initial = (r.inventory.return_value[0], [self.a1()], 50)
            r.inventory.return_value = initial
            r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
            if cause == 'other_instance':
                r.inventory.side_effect = [initial, (initial[0], initial[1] + [NS(id='other')], 100)]
            else:
                r.compute.list_shapes.return_value.data[0].billing_type = 'PAID'
            with self.assertRaises(Stop):
                r.run('retry')
            r.compute.update_instance.assert_not_called()
            r.compute.launch_instance.assert_not_called()

    def test_followup_timeout_preserves_instance_and_stops(self):
        r = self.make_retry(phase='created', instance_id='created', followup_checks=4)
        r.inventory.return_value = (r.inventory.return_value[0], [self.a1('PROVISIONING')], 50)
        r.compute.list_vnic_attachments.return_value = oci.response.Response(200, {}, [], None)
        r.run('retry')
        self.assertEqual(r.store.state['phase'], 'stopped')
        r.compute.update_instance.assert_not_called()
        r.compute.launch_instance.assert_not_called()

    def test_ad_start_rotates_and_request_gaps_are_at_least_sixty_seconds(self):
        r = self.make_retry(next_ad_offset=1)
        r.compute.launch_instance.side_effect = capacity()
        with patch.dict(os.environ, RETRY_ENABLED='true'), patch('retry.time.monotonic', return_value=0):
            r.run('retry')
        self.assertEqual([c.args[0].availability_domain for c in r.compute.launch_instance.call_args_list],
                         ['AD-2', 'AD-3', 'AD-1'])
        self.assertEqual(r.store.state['next_ad_offset'], 2)
        self.assertEqual([c.args[0] for c in self.sleeper.call_args_list], [60, 60])
        results = [x for x in r.store.state['attempt_log'] if x['result'] == 'capacity']
        self.assertEqual(len(results), 3)
        self.assertTrue(all(x['status'] == 500 and x['error'] == 'Out of host capacity.'
                            and x['beijing'].endswith('+08:00') for x in results))

    def test_result_history_is_bounded_and_error_does_not_dump_request(self):
        r = self.make_retry(attempt_log=[{'old': n} for n in range(40)])
        error = TimeoutError('SECRET-EXCEPTION-CONTENT')
        with patch('builtins.print') as output:
            r.record('AD-1', SHAPE, 'failed_or_unknown', error)
        self.assertEqual(len(r.store.state['attempt_log']), 40)
        self.assertNotIn('SECRET-EXCEPTION-CONTENT', str(output.call_args))

    def test_check_mode_with_existing_a1_never_resizes(self):
        r = self.make_retry(phase='created', instance_id='created')
        r.inventory.return_value = (r.inventory.return_value[0], [self.a1()], 50)
        r.run('check')
        r.compute.update_instance.assert_not_called()
        r.compute.launch_instance.assert_not_called()

    def test_missing_recorded_instance_never_replaced(self):
        r = self.make_retry(phase='created', instance_id='created')
        with self.assertRaises(Stop):
            r.run('retry')
        r.compute.launch_instance.assert_not_called()



if __name__ == '__main__':
    unittest.main()
