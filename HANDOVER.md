# 甲骨文免费服务器抢机交接给 Luna

本文件按账户所有者要求公开。核验日期：2026-11-22。仅发布 SSH 公钥及必要的脱敏交接信息，不包含私钥、密码、API 密钥、真实邮箱、云资源编号或公网 IP。

**交接状态：文件准备不等于旧任务已停止。核验时私有 GitHub 工作流仍启用，`RETRY_ENABLED=true`；本次没有修改其启停状态，也没有发起创建请求。Luna 正式创建前必须先完成下文的单执行端切换。**

## 1. 原来建机器使用的 SSH 公钥

下列完整单行来自原任务保存的 `.pub` 文件，保留原注释。创建实例时填入 `ssh_authorized_keys` / `SSH_PUBLIC_KEY`，不要重新生成或替换。

```text
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDSwphWbDumEtMDqhDTVjvUDnffPfx/qZVDQO1jvYhT8GbEqr181CyLsI5agTJH6aZTBSVkSWv2H7ep2O+PGEV5jo1Z92SMc38mdL550TI5+P1EXUnfv3ixnHuNDrlOlfL43DeQtZWrMxCh+5Bw4mJ7Lcax5iWljUhICbwRdRi1o2fDi6bjLNKVc8IuWG0RJx6EdGchBNw6dM5zxWbdb8bOMuol1b08Ip5IHalVMHZaLYUo+lk0TVSYR5ZOHRCvU9n7xednRdqeoqBoPJEVBHBKcW4pj4li3mqaS+fcnJf2cEsWSl05mBnIGQraNXP7OWXyZUDZo3JPmcKLhbtnULol ssh-key-2026-11-13
```

- 类型：RSA 2048 位。
- SHA256 指纹：`SHA256:dGFbe9XupkUUBbHxo0BQU3wfIUaYmRkuffccfl1zHBI`。
- 这是服务器登录公钥，不是 OCI API 签名密钥；公钥不能用于签名 OCI 创建请求，也不能单独 SSH 登录。
- 对应 SSH 私钥仍由账户所有者保管，本交接没有读取或上传私钥。Luna 创建机器只需要公钥；如需验证 SSH 登录，另由持有私钥的所有者执行或通过安全途径授权。
- GitHub Secrets 的已保存明文无法从界面回读；这里核验的是原任务本地保存公钥，不把掩码 `***` 当成密钥内容验证。

## 2. 要停止的是哪个 GitHub 定时任务

| 项目 | 精确值 |
|---|---|
| 生产私有仓库 | `jyj081351-pixel/oci-free-retry` |
| Actions 显示名称 | **免费最低配置重试** |
| 工作流文件 | `.github/workflows/retry.yml` |
| 工作流文件 ID | `retry.yml` |
| 作业 ID | `retry` |
| 分支 | `main` |
| 计划表达式（UTC） | `23 0,6-15 * * *` |
| 北京时间计划 | 每天 08:23，以及 14:23–23:23 每小时一轮，共 11 轮；实际调度可能延迟 |
| 重试开关（核验时） | Repository variable `RETRY_ENABLED=true` |
| 定时事件运行模式 | `MODE=retry` |
| 并发组 | `oci-single-free-instance`，`cancel-in-progress: false` |

[打开正确的私有工作流](https://github.com/jyj081351-pixel/oci-free-retry/actions/workflows/retry.yml)（需要有权限的 GitHub 登录）。在此页面右上角 **Workflow options → Disable workflow** 停止后续调度。不要停错到本公开仓库；公开仓库只是代码参考，没有生产定时工作流。

**切换顺序：**先停用上述私有工作流，确认没有 queued / in_progress / waiting 的旧运行；等待已发出的请求结果明确，再查实际 OCI 实例与系统盘及私有状态。发现已有实例或不明确的创建结果时只跟进该请求，禁止另建。确认旧执行端彻底停止且没有待核实资源后，才启动 Luna 的唯一执行端。

只把 `RETRY_ENABLED` 改为 `false` 不能终止已经运行的作业；突然取消正在创建的作业也不能证明云端请求已取消。GitHub 的并发组只保护该仓库工作流，不会自动锁住 Luna 的外部进程。

## 3. 公开副本是不是最新版，有没有私有独有改动

本次从两个仓库 `main` 在线读取以下四个文件，逐字节比较，全部一致。

| 私有仓库文件 | 本公开仓库文件 | 比较 |
|---|---|---|
| `retry.py` | `retry.py` | 完全一致 |
| `test_retry.py` | `test_retry.py` | 完全一致 |
| `requirements.txt` | `requirements.txt` | 完全一致，`oci==2.191.0` |
| `.github/workflows/retry.yml` | `retry-workflow.example.yml` | 内容完全一致，仅位置/名称不同 |

- 本次核验的生产运行代码版本：`caf708d105efc9c25d13e88fcc2ed4a15f201c85`，最近运行 #91、#92、#93 使用该版本。
- 添加本交接前，公开副本版本：`c88ce0d741083ae2dd4c56c85dd1f5b4a120042f`。公开副本有独立干净历史，所以提交哈希不同不代表代码不同。
- `retry.py` SHA256：`64fd70e2eb141b6681b298bef34b54b82100ed7b98d6e1f74683197ae65870ea`。
- 没有发现私有独有的抢机业务逻辑。私有仓库的工作流目录只包含 `retry.yml`。
- 仓库级差异：README、安全说明及忽略规则用途不同；私有仓库有真实 Actions 配置、Secrets、变量、运行历史和状态 Issue。这些不是公开副本自带的运行环境。本公开仓库继续保持参考用途，不启用抢机。

### 当前代码的关键行为

- 只用 `us-chicago-1` 主区域；目标只允许一台 `us-chicago-gateway-01`。
- 先试 `VM.Standard.A1.Flex`，**1 OCPU / 6 GB**，官方 Ubuntu 24.04 ARM 镜像，系统盘 50 GB / 10 VPU，复用已有合适子网，请求公网 IPv4。
- 每轮按轮换后的顺序尝试 AD-1、AD-2、AD-3，不指定故障域 FD；前一个创建请求返回后至少等 60 秒再发下一个请求。
- E2 Micro 只有在实际列表提供免费机型、兼容的官方 x86 镜像及剩余额度可确认时才作为备用；每轮最多再尝试一个合格 E2。不能为尝试 E2 自动改成收费机型。
- A1 创建成功立即记下实例并通知、停止新建；后续只跟进同一实例，Running 后最多尝试一次原地升为 2 OCPU / 12 GB。失败或结果不明保留原实例，不删除重建。E2 成功则保留固定配置，不再建 A1。
- 每次提交前检查实际资源；不明请求先阻断，不能当缺货无脑重试。免费资格不明时停止，不使用试用赠金代替 Always Free、不升级付费账户。
- 260 秒脚本超时、5 分钟 Actions 作业上限、后续实例跟进次数有界；它不是无限循环脚本。
- 本段描述核验代码行为，不替代执行时对官方免费规则及账号实际额度的核验。

### Luna 迁移时不能漏掉的依赖

`retry.py` 使用 `GitHubState` 将状态写入私有 Issue，并通过 GitHub 提及通知所有者；成功/异常停止时调用 GitHub 接口禁用 `retry.yml`。直接放进云电脑运行，不会自动获得这些能力。

- 当前状态入口：[私有 Issue #2](https://github.com/jyj081351-pixel/oci-free-retry/issues/2)，标题 `[自动状态] 甲骨文免费服务器`。必须安全读取/衔接现有状态，不能从一个空记录重新开始。
- 需要 `GITHUB_REPOSITORY`、有相应私有仓库权限的 `GH_TOKEN`，以及 `OCI_TENANCY`、`OCI_USER`、`OCI_FINGERPRINT`、`OCI_KEY_CONTENT`、`OCI_COMPARTMENT`、`OCI_SUBNET`、`OCI_IMAGE`、`SSH_PUBLIC_KEY`。这里只有变量名称，不提供凭据值；凭据只经所有者授权的安全渠道配置。原 Actions 临时 token 不能拿来作长期凭据。
- 原通知测试曾由所有者确认收件；迁移后的邮件链路仍需单独验证，不能假定自动继承。脚本中的 `notify-test` 会停止对应 GitHub 工作流，不要在不了解副作用时调用。
- 若沿用脚本，成功时它只停 GitHub 工作流；Luna 自己的循环/计划任务也必须停止。状态中的 stopped / created / launch_pending 必须跨重启持久保存，并确保最多一个执行进程。
- 真实云配置、状态、日志、实例编号和将来的公网 IP 必须继续私密，不能写入本公开仓库的 Issue 或提交历史。

## 4. 最近运行反馈，有没有“差点成功”

核验范围：最近 #91、#93 的实际“云端检查或重试”步骤、本地已核验 #92 实际日志，以及本次在线读取的私有状态记录。#89、#90 另由 Actions 列表和状态记录交叉确认，未在本次逐条打开完整日志。下表时间为真实 A1 创建请求时段，均为北京时间（UTC+8），不是计划触发时间。

| 运行 | 事件 | 总耗时 | A1 请求时段（北京时间） | AD 顺序 | 结果 |
|---|---|---|---|---|---|
| [#89](https://github.com/jyj081351-pixel/oci-free-retry/actions/runs/44908743533) | schedule | 2 分 54 秒 | 11-21 21:25:38–21:27:44 | 3 → 1 → 2 | 三个 AD 缺货；E2 not_offered |
| [#90](https://github.com/jyj081351-pixel/oci-free-retry/actions/runs/44917709332) | schedule | 2 分 54 秒 | 11-21 23:10:35–23:12:40 | 1 → 2 → 3 | 三个 AD 缺货；E2 not_offered |
| [#91](https://github.com/jyj081351-pixel/oci-free-retry/actions/runs/44922817957) | schedule | 2 分 45 秒 | 11-22 00:08:01–00:10:07 | 2 → 3 → 1 | 三个 AD 缺货；E2 not_offered |
| [#92](https://github.com/jyj081351-pixel/oci-free-retry/actions/runs/44972243440) | schedule | 2 分 53 秒 | 11-22 09:37:10–09:39:15 | 3 → 1 → 2 | 三个 AD 缺货；E2 not_offered |
| [#93](https://github.com/jyj081351-pixel/oci-free-retry/actions/runs/44999157877) | schedule | 2 分 52 秒 | 11-22 15:39:11–15:41:17 | 1 → 2 → 3 | 三个 AD 缺货；E2 not_offered |

具体判断：

1. **错误没有变化。** A1 都是 `ServiceError / HTTP 500 / InternalError / Out of host capacity.`。E2 是三个机房完整机型列表均没有提供 `VM.Standard.E2.1.Micro`，所以跳过，而不是 E2 创建请求已提交后缺货。
2. **没有发现“差点成功”的证据。** `request_pending` 是脚本在发送请求前持久化的防重复标记；上述请求随后均明确返回 capacity，不表示已获名额或进入 OCI Provisioning。
3. 最新私有状态更新时间为 `2026-11-22T07:41:19.986184+00:00`（北京时间 15:41:19），`phase=ready`、`last_result=Out of host capacity`，没有记录实例 ID。此结论基于最新 GitHub 日志和状态；本次没有独立直接查询 OCI 清单，接管前仍需重新查云端资源。
4. #93 两次请求起始间隔约 63.02、62.72 秒；#92 约 62.50、62.37 秒，确认不是只读检查。#80 的历史 55 秒运行也确实试了三个 AD，只是旧版没有 60 秒等待；不能仅凭时长判断是否真抢。
5. GitHub 的绿色 Success 表示脚本正常处理完本轮，不代表服务器创建成功；模拟测试步骤里的成功输出不能算真实云端创建成功。
6. #91、#93 页面有 `actions/checkout` 使用 Node.js 20、被平台强制转为 Node.js 24 的弃用提醒；本次作业成功且真实请求正常，它不是当前缺货原因。后续维护可核验升级 checkout，但本交接不改生产代码。

## 5. 接管完成的判定

Luna 应先核验旧工作流已停、无活动运行、实际资源和状态一致，再以唯一执行端启动。每次保留时间、AD、配置、请求结果和错误；成功后持久记录同一实例、停止所有创建重试、验证 Running 与公网 IP，并邮件通知所有者。手机/电脑 VPN 或代理连接仍未配置，不得把“实例创建成功”写成“各设备已经可以使用”。
