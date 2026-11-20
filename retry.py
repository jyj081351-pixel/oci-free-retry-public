"""Bounded A1 attempts, then one eligible E2 fallback. No paid resources.

Create the network and verify the official Ubuntu image during setup. The job
only launches A1 1 OCPU / 6 GB or fixed E2 Micro, with a 50 GB / 10 VPU disk.
GitHub issue state is persisted BEFORE any launch request. An interrupted or
ambiguous launch blocks further launches until a human resolves it.
"""

import json
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

import oci

REGION = 'us-chicago-1'
SHAPE = 'VM.Standard.A1.Flex'
E2_SHAPE = 'VM.Standard.E2.1.Micro'
NAME = 'us-chicago-gateway-01'
TITLE = '[自动状态] 甲骨文免费服务器'
WORKFLOW = 'retry.yml'
REQUEST_GAP_SECONDS = 60
LOG_TAIL = 40

PREFIX = '仅限免费最低配置；请勿手工修改下方自动状态。\n\n```json\n'


class Stop(Exception):
    """Safety invariant failed: notify once and disable this workflow."""


def require(condition, message):
    if not condition:
        raise Stop(message)


def all_items(fn, *args, **kwargs):
    rows = []
    seen = set()
    while True:
        response = fn(*args, **kwargs)
        rows.extend(response.data)
        page = response.headers.get('opc-next-page')
        if not page:
            return rows
        require(page not in seen, '资源分页重复，无法可靠确认全部资源。')
        seen.add(page)
        kwargs['page'] = page


def is_capacity_error(exc):
    return (isinstance(exc, oci.exceptions.ServiceError)
            and exc.status == 500
            and bool(re.search(r'out of (?:host )?capacity', exc.message, re.I)))


def require_a1_capacity(shape, cpu, memory):
    require(shape.shape == SHAPE and shape.billing_type == 'LIMITED_FREE',
            'A1 机型未标记 LIMITED_FREE，停止修改。')
    c, m = shape.ocpu_options, shape.memory_options
    require(c is not None and m is not None and c.min is not None
            and m.min_in_g_bs is not None and m.min_per_ocpu_in_gbs is not None,
            '配置范围不完整，无法核验。')
    require(c.min <= cpu and (c.max is None or cpu <= c.max)
            and m.min_in_g_bs <= memory and (m.max_in_g_bs is None or memory <= m.max_in_g_bs)
            and m.min_per_ocpu_in_gbs <= memory / cpu
            and (m.max_per_ocpu_in_gbs is None or memory / cpu <= m.max_per_ocpu_in_gbs),
            '机房不支持所需配置，停止而非自动改变配置。')


def launch_details(compartment, subnet, image, public_key, ad, shape=SHAPE):
    require(shape in (SHAPE, E2_SHAPE), '机型不在免费白名单内。')
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=ad, compartment_id=compartment, display_name=NAME,
        shape=shape,
        shape_config=(oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=6)
                      if shape == SHAPE else None),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image, boot_volume_size_in_gbs=50, boot_volume_vpus_per_gb=10),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet, assign_public_ip=True, assign_ipv6_ip=False),
        metadata={'ssh_authorized_keys': public_key},
        freeform_tags={'managed-by': 'oci-free-retry'},
        instance_options=oci.core.models.InstanceOptions(are_legacy_imds_endpoints_disabled=True))


class GitHubState:
    def __init__(self, title=TITLE):
        self.title = title
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']
        self.number = None
        self.state = {}

    def api(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request('https://api.github.com/repos/' + self.repo + path,
                      method=method, data=data,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json',
                               'X-GitHub-Api-Version': '2022-11-28'})
        with urlopen(req, timeout=15) as response:
            body = response.read()
        return json.loads(body) if body else None

    def load(self):
        matches = []
        page = 1
        while True:
            rows = self.api('GET', f'/issues?state=all&per_page=100&page={page}')
            matches += [x for x in rows if x['title'] == self.title and 'pull_request' not in x]
            if len(rows) < 100:
                break
            page += 1
        require(len(matches) <= 1, '发现多个自动状态记录，停止以免重复创建。')
        if matches:
            issue = matches[0]
            self.number = issue['number']
            body = issue.get('body') or ''
            require(body.startswith(PREFIX) and body.endswith('\n```'), '状态记录格式已改变，停止自动创建。')
            self.state = json.loads(body[len(PREFIX):-4])
            require(self.state.get('version') == 1, '未知状态记录版本。')
        else:
            self.state = {'version': 1, 'phase': 'ready'}
            issue = self.api('POST', '/issues', {'title': self.title, 'body': self.body()})
            self.number = issue['number']
        return self.state

    def body(self):
        return PREFIX + json.dumps(self.state, ensure_ascii=False, indent=2) + '\n```'

    def save(self, **changes):
        self.state.update(changes, updated_at=datetime.now(timezone.utc).isoformat())
        self.api('PATCH', f'/issues/{self.number}', {'body': self.body()})

    def disable(self):
        self.api('PUT', f'/actions/workflows/{WORKFLOW}/disable')

    def notify_created(self, message):
        if not self.state.get('created_notified'):
            owner = self.repo.split('/')[0]
            self.api('POST', f'/issues/{self.number}/comments', {
                'body': f'@{owner}\n\n{message}\n\n创建重试已停止；仅跟进同一实例的启动和一次免费升配。VPN/代理尚未配置。'})
            self.save(created_notified=True)

    def notify_and_stop(self, message):
        # Persist stop before sending. If a later call fails, the next run still
        # cannot launch. Repeat notifications are suppressed after acknowledgement.
        self.save(phase='stopped', outcome=message)
        if not self.state.get('notified'):
            owner = self.repo.split('/')[0]
            self.api('POST', f'/issues/{self.number}/comments', {
                'body': f'@{owner}\n\n{message}\n\n自动创建已停止。VPN/代理和各设备连接尚未配置。'})
            self.save(notified=True)
        self.disable()


class Retry:
    def __init__(self, config, store, compartment, subnet, image, public_key):
        self.config, self.store = config, store
        self.compartment, self.subnet, self.image, self.public_key = compartment, subnet, image, public_key
        opts = {'timeout': (5, 12), 'retry_strategy': oci.retry.NoneRetryStrategy()}
        self.identity = oci.identity.IdentityClient(config, **opts)
        self.compute = oci.core.ComputeClient(config, **opts)
        self.network = oci.core.VirtualNetworkClient(config, **opts)
        self.block = oci.core.BlockstorageClient(config, **opts)

    def inventory(self):
        tenancy = self.config['tenancy']
        regions = self.identity.list_region_subscriptions(tenancy).data
        require(any(r.region_name == REGION and r.is_home_region for r in regions), 'Chicago 不是主区域，禁止创建。')
        # ANY inventory permission error aborts. Never interpret failed reads as zero.
        compartments = all_items(self.identity.list_compartments, tenancy,
                                 compartment_id_in_subtree=True, access_level='ANY')
        ids = [tenancy] + [c.id for c in compartments if c.lifecycle_state == 'ACTIVE']
        require(self.compartment in ids, '目标 compartment 不在当前租户内。')
        ads = self.identity.list_availability_domains(tenancy).data
        instances = []
        volume_gb = 0
        for cid in ids:
            instances += [i for i in all_items(self.compute.list_instances, cid)
                          if i.lifecycle_state != 'TERMINATED']
            volume_gb += sum(v.size_in_gbs for v in all_items(self.block.list_volumes, compartment_id=cid)
                             if v.lifecycle_state != 'TERMINATED')
            for ad in ads:
                volume_gb += sum(v.size_in_gbs for v in all_items(
                    self.block.list_boot_volumes, availability_domain=ad.name, compartment_id=cid)
                    if v.lifecycle_state != 'TERMINATED')
        return ads, instances, volume_gb

    def preflight(self):
        subnet = self.network.get_subnet(self.subnet).data
        require(subnet.lifecycle_state == 'AVAILABLE' and not subnet.prohibit_public_ip_on_vnic,
                '子网不可用或不允许公网 IP。')
        require(subnet.compartment_id == self.compartment, '子网与目标 compartment 不一致。')
        require(subnet.availability_domain is None, '必须使用跨机房区域子网。')
        image = self.compute.get_image(self.image).data
        require(image.compartment_id is None and image.operating_system == 'Canonical Ubuntu'
                and image.operating_system_version == '24.04 Minimal aarch64'
                and 'minimal' in image.display_name.lower()
                and 'aarch64' in image.display_name.lower()
                and image.lifecycle_state == 'AVAILABLE', '镜像必须是可用的官方 Ubuntu 24.04 Minimal ARM 平台镜像。')
        require(image.size_in_mbs <= 50 * 1024, '镜像需要更大系统盘，禁止自动增加配置。')
        compat = self.compute.get_image_shape_compatibility_entry(self.image, SHAPE).data
        require((compat.memory_constraints is None or compat.memory_constraints.min_in_gbs is None or compat.memory_constraints.min_in_gbs <= 6)
                and (compat.ocpu_constraints is None or compat.ocpu_constraints.min is None or compat.ocpu_constraints.min <= 1),
                '此镜像不支持 1 核 6 GB，停止而非自动升配。')

    def record(self, ad, shape, result, exc=None, operation='launch'):
        now = datetime.now(timezone.utc)
        entry = {'utc': now.isoformat(),
                 'beijing': now.astimezone(timezone(timedelta(hours=8))).isoformat(),
                 'ad': ad, 'shape': shape, 'operation': operation, 'result': result,
                 'requested_config': ('2 OCPU/12 GB' if operation == 'resize_2c12g'
                                      else '1 OCPU/6 GB' if shape == SHAPE and operation == 'launch'
                                      else 'fixed E2 Micro' if shape == E2_SHAPE else 'unchanged'),
                 'run_id': os.environ.get('GITHUB_RUN_ID', 'local')}
        if exc is not None:
            entry['error_type'] = type(exc).__name__
            if isinstance(exc, oci.exceptions.ServiceError):
                entry.update(status=exc.status, code=str(exc.code)[:80],
                             error=re.sub(r'[\r\n]+', ' ', exc.message)[:300])
        # All results stay in each run's logs; keep a bounded recent tail in
        # the issue to stay below GitHub's issue-body limit. Never log config.
        print('ATTEMPT ' + json.dumps(entry, ensure_ascii=False), flush=True)
        self.store.save(attempt_log=(self.store.state.get('attempt_log', []) + [entry])[-LOG_TAIL:])

    def pace(self):
        previous = getattr(self, '_last_request_finished', None)
        if previous is not None:
            delay = REQUEST_GAP_SECONDS - (time.monotonic() - previous)
            if delay > 0:
                time.sleep(delay)

    def finish_instance(self, instance):
        # Record the identity before any other request. No code path creates
        # a replacement, even if startup, IP lookup or resizing fails.
        self.store.save(phase='created', instance_id=instance.id,
                        instance_state=instance.lifecycle_state)
        ips = []
        try:
            vnics = all_items(self.compute.list_vnic_attachments, instance.compartment_id,
                              instance_id=instance.id)
            for attachment in vnics:
                if attachment.lifecycle_state == 'ATTACHED':
                    vnic = self.network.get_vnic(attachment.vnic_id).data
                    if vnic.public_ip:
                        ips.append(vnic.public_ip)
        except Exception:
            pass
        self.store.save(public_ips=ips)
        self.record(getattr(instance, 'availability_domain', 'unknown'), instance.shape,
                    instance.lifecycle_state, operation='instance_followup')
        cfg = getattr(instance, 'shape_config', None)
        cpu, memory = getattr(cfg, 'ocpus', None), getattr(cfg, 'memory_in_gbs', None)
        message = (f'已发现/创建服务器 {NAME}，状态：{instance.lifecycle_state}。\n'
                   f'实例：{instance.id}\n公网 IP：{", ".join(ips) if ips else "尚待确认"}。\n'
                   f'区域：{REGION}；机型：{instance.shape}；OCPU：{cpu}；内存 GB：{memory}。\n'
                   '系统盘目标 50 GB / 10 VPU；SSH 登录仍需核验。')
        if instance.shape == E2_SHAPE:
            self.store.notify_and_stop(message + '\nE2 使用固定小配置，不另建 ARM 实例。')
            return
        require(instance.shape == SHAPE, '已有实例机型不在白名单内；保留实例并停止。')
        if (cpu, memory) == (2, 12) and instance.lifecycle_state == 'RUNNING':
            self.store.save(resize_status='complete')
            self.store.notify_and_stop(message + '\n同一台 A1 已达到 2 核 12 GB，并处于 RUNNING。')
            return
        # An unrecognized/preexisting configuration is never silently changed.
        if (cpu, memory) not in ((1, 6), (2, 12)):
            self.store.notify_and_stop(message + '\n配置与升配前提不符，未自动修改。')
            return
        self.store.notify_created(message)
        followups = self.store.state.get('followup_checks', 0) + 1
        self.store.save(followup_checks=followups)
        if followups > 4:
            self.store.notify_and_stop(message + '\n启动/升配仍未确认完成，已停止自动跟进；保留此实例，请人工核验。')
            return
        if instance.lifecycle_state in ('STOPPED', 'TERMINATING', 'TERMINATED'):
            self.store.notify_and_stop(message + '\n实例状态需要人工核验，未重建。')
            return
        if instance.lifecycle_state != 'RUNNING' or self.store.state.get('resize_attempted'):
            print('已有实例；只等待此实例启动/升配，不再创建或重复升配。')
            return
        # Re-read tenancy-wide inventory immediately before increasing resources.
        _, instances, gb = self.inventory()
        require(len(instances) == 1 and instances[0].id == instance.id and gb <= 200,
                '升配前资源核对不通过；保留实例，不自动升配。')
        self.preflight()
        shapes = all_items(self.compute.list_shapes, self.compartment,
                           availability_domain=instance.availability_domain, image_id=self.image)
        candidates = [s for s in shapes if s.shape == SHAPE]
        require(len(candidates) == 1 and candidates[0].billing_type == 'LIMITED_FREE',
                '升配免费资格未确认，保留当前实例。')
        require_a1_capacity(candidates[0], 2, 12)
        # Current official allowance: 2 OCPU / 12 GB total; one instance only.
        detail = oci.core.models.UpdateInstanceDetails(
            shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12))
        self.store.save(resize_attempted=True, resize_status='pending')
        self.record(instance.availability_domain, SHAPE, 'request_pending', operation='resize_2c12g')
        try:
            self.compute.update_instance(instance.id, detail, retry_strategy=oci.retry.NoneRetryStrategy())
        except Exception as exc:
            self.record(instance.availability_domain, SHAPE, 'failed_or_unknown', exc, 'resize_2c12g')
            self.store.save(resize_status='failed_or_unknown')
            raise Stop('已创建实例，升配失败或结果未明确。保留同一实例，不重建；请核对实际配置和运行状态。') from None
        self.record(instance.availability_domain, SHAPE, 'accepted', operation='resize_2c12g')
        print('同一台 A1 的 2 核 12 GB 升配请求已受理；下一轮只核验，不再创建。')

    def e2_candidate(self, ads):
        # Do not filter by the A1 ARM image: that would hide an x86-only shape.
        eligible = []
        for ad in ads[:3]:
            shapes = all_items(self.compute.list_shapes, self.compartment,
                               availability_domain=ad.name)
            for shape in shapes:
                if shape.shape != E2_SHAPE:
                    continue
                require(shape.billing_type == 'ALWAYS_FREE', 'E2 未标记 ALWAYS_FREE，停止创建。')
                require(shape.is_flexible is False and shape.memory_in_gbs == 1
                        and shape.ocpus is not None and 0 < shape.ocpus <= 1,
                        'E2 固定规格不符合免费 Micro 配置，停止创建。')
                eligible.append(ad)
        if not eligible:
            self.record('AD-1/AD-2/AD-3', E2_SHAPE, 'not_offered', operation='eligibility')
            print('E2: 三个机房的完整机型列表均未提供免费 Micro，跳过。')
            return None
        images = all_items(self.compute.list_images, compartment_id=self.compartment,
                           operating_system='Canonical Ubuntu',
                           operating_system_version='24.04 Minimal', shape=E2_SHAPE,
                           sort_by='TIMECREATED', sort_order='DESC')
        images = [i for i in images if i.compartment_id is None
                  and i.operating_system == 'Canonical Ubuntu'
                  and i.operating_system_version == '24.04 Minimal'
                  and i.display_name.startswith('Canonical-Ubuntu-24.04-Minimal-')
                  and not any(a in i.display_name.lower() for a in ('aarch64', 'arm64'))
                  and i.lifecycle_state == 'AVAILABLE'
                  and i.size_in_mbs is not None and i.size_in_mbs <= 50 * 1024]
        if not images:
            self.record(eligible[0].name, E2_SHAPE, 'no_compatible_official_image', operation='eligibility')
            print('E2: 暂无合适的官方 Ubuntu 24.04 Minimal x86 镜像，跳过。')
            return None
        image = images[0]
        compat = self.compute.get_image_shape_compatibility_entry(image.id, E2_SHAPE).data
        require((compat.memory_constraints is None or compat.memory_constraints.min_in_gbs is None
                 or compat.memory_constraints.min_in_gbs <= 1)
                and (compat.ocpu_constraints is None or compat.ocpu_constraints.min is None
                     or compat.ocpu_constraints.min <= 1), 'E2 官方镜像不兼容最低配置。')
        print(f'E2: 已核对 ALWAYS_FREE、1 GB、官方 x86 镜像；候选机房 {eligible[0].name}。')
        # E2 is offered in a single AD. Never multiply E2 attempts across ADs.
        return eligible[0].name, image.id

    def run(self, mode):
        state = self.store.state
        if state.get('phase') == 'stopped':
            self.store.notify_and_stop(state.get('outcome', '任务此前已停止。'))
            return
        ads, instances, volume_gb = self.inventory()
        targets = [i for i in instances if i.display_name == NAME or i.id == state.get('instance_id')]
        require(len(targets) <= 1, '发现多个同名服务器，需要人工核对；不会再创建。')
        if targets:
            if mode == 'check':
                print('已存在目标实例；检查模式不修改、不创建。')
            else:
                self.finish_instance(targets[0])
            return
        require(state.get('phase') not in ('launch_pending', 'created'),
                '上次创建结果未明确或已记录过实例；停止重试，需人工核对。')
        require(len(ads) == 3, '主区域三个机房列表不完整，停止创建。')
        require(not instances, '主区域已有其他实例；无法保证剩余免费额度，停止创建。')
        require(volume_gb + 50 <= 200, '现有系统盘和数据盘加本次 50 GB 超过免费 200 GB，停止创建。')
        self.preflight()
        e2 = self.e2_candidate(ads)
        if mode == 'check':
            print('只读检查通过：Chicago 主区域、官方镜像、公开子网、50 GB / 10 VPU；未指定 FD。未创建实例。')
            return
        require(os.environ.get('RETRY_ENABLED') == 'true', '云端重试尚未启用，未创建资源。')
        offset = state.get('next_ad_offset', 0) % len(ads)
        ordered_ads = ads[offset:] + ads[:offset]
        self.store.save(next_ad_offset=(offset + 1) % len(ads))
        for ad in ordered_ads[:3]:
            shapes = all_items(self.compute.list_shapes, self.compartment,
                               availability_domain=ad.name, image_id=self.image)
            selected = [s for s in shapes if s.shape == SHAPE]
            if not selected:
                self.record(ad.name, SHAPE, 'not_offered', operation='eligibility')
                continue
            shape = selected[0]
            require_a1_capacity(shape, 1, 6)
            self.pace()
            token = str(uuid.uuid4())
            self.store.save(phase='launch_pending', ad=ad.name, shape=SHAPE,
                            image_id=self.image, retry_token=token)
            self.record(ad.name, SHAPE, 'request_pending')
            try:
                instance = self.compute.launch_instance(
                    launch_details(self.compartment, self.subnet, self.image, self.public_key, ad.name),
                    opc_retry_token=token, retry_strategy=oci.retry.NoneRetryStrategy()).data
            except Exception as exc:
                self._last_request_finished = time.monotonic()
                self.record(ad.name, SHAPE, 'capacity' if is_capacity_error(exc) else 'failed_or_unknown', exc)
                if not is_capacity_error(exc):
                    raise Stop('创建请求未得到明确缺货结果；已停止，禁止盲目重试。') from None
                self.store.save(phase='ready', last_result='Out of host capacity', ad=ad.name)
                print(f'{ad.name}: Out of host capacity')
                continue
            self.finish_instance(instance)
            return
        if e2:
            # Explicit A1 capacity errors must not leave an instance or disk
            # before crossing architectures. Recheck actual cloud resources.
            _, current, current_gb = self.inventory()
            targets = [i for i in current if i.display_name == NAME]
            require(len(targets) <= 1, '发现多个目标实例，停止创建。')
            if targets:
                self.finish_instance(targets[0])
                return
            require(not current and current_gb == volume_gb,
                    'A1 尝试后云端资源发生变化，停止 E2 创建以免重复。')
            ad, image = e2
            self.pace()
            token = str(uuid.uuid4())
            self.store.save(phase='launch_pending', ad=ad, shape=E2_SHAPE,
                            image_id=image, retry_token=token)
            self.record(ad, E2_SHAPE, 'request_pending')
            try:
                instance = self.compute.launch_instance(
                    launch_details(self.compartment, self.subnet, image, self.public_key, ad, E2_SHAPE),
                    opc_retry_token=token, retry_strategy=oci.retry.NoneRetryStrategy()).data
            except Exception as exc:
                self._last_request_finished = time.monotonic()
                self.record(ad, E2_SHAPE, 'capacity' if is_capacity_error(exc) else 'failed_or_unknown', exc)
                if not is_capacity_error(exc):
                    raise Stop('E2 创建结果不明确；已停止，禁止盲目重试。') from None
                self.store.save(phase='ready', last_result='Out of host capacity', ad=ad)
                print(f'{ad}: E2 Out of host capacity')
            else:
                self.finish_instance(instance)
                return
        print('本轮无可用免费 A1/E2 容量，等待下一次计划运行。')


def main():
    mode = os.environ.get('MODE', 'check')
    require(mode in ('check', 'retry', 'notify-test'), '未知运行模式。')
    store = GitHubState('[通知测试] 甲骨文免费服务器' if mode == 'notify-test' else TITLE)
    store.load()
    if mode == 'notify-test':
        store.notify_and_stop('通知通道及自动停止测试成功。本次没有调用甲骨文，也没有创建服务器。这是测试通知。')
        print('测试通知已提交 GitHub，工作流已停用；邮箱实际收件需另行确认。')
        return
    try:
        names = ('OCI_TENANCY', 'OCI_USER', 'OCI_FINGERPRINT', 'OCI_KEY_CONTENT',
                 'OCI_COMPARTMENT', 'OCI_SUBNET', 'OCI_IMAGE', 'SSH_PUBLIC_KEY')
        require(all(os.environ.get(n, '').strip() for n in names), '配置凭据尚未完整，未访问或创建云资源。')
        public_key = os.environ['SSH_PUBLIC_KEY'].strip()
        require(public_key.startswith(('ssh-rsa ', 'ssh-ed25519 ')) and 'PRIVATE KEY' not in public_key,
                'SSH 公钥格式错误，禁止上传私钥。')
        config = {'tenancy': os.environ['OCI_TENANCY'], 'user': os.environ['OCI_USER'],
                  'fingerprint': os.environ['OCI_FINGERPRINT'], 'key_content': os.environ['OCI_KEY_CONTENT'],
                  'region': REGION}
        oci.config.validate_config(config)
        Retry(config, store, os.environ['OCI_COMPARTMENT'], os.environ['OCI_SUBNET'],
              os.environ['OCI_IMAGE'], public_key).run(mode)
    except Exception as exc:
        # Never dump SDK config, API private keys, environment variables or raw
        # HTTP responses into GitHub logs/notifications.
        message = str(exc) if isinstance(exc, Stop) else f'运行遇到 {type(exc).__name__}，需要人工检查。'
        store.notify_and_stop(message)
        print(message)


if __name__ == '__main__':
    def timed_out(_signum, _frame):
        raise Stop('本轮达到 260 秒时间限制，已停止；需核对是否有未完成的请求。')
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(260)
    main()
