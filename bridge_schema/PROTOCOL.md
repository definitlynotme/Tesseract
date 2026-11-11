# 侧车 IPC 协议合同 v1 全集（P2 / P3 冻结合同）

本文件是 Python 侧车、Tauri Rust 外壳和页面的规范性合同。P2 依据优先级为 `决定_P2_ASK02.md`、T-P2-01、方案 §0、ASK-02 §2/§5/§8、`P2_输入_Fallback会话.md`；P1 已有传输和生命周期合同保留。§10–§16 冻结 v1 全集，优先于前文的 P1 范围叙述。合同登记不等于实现开放：`hello.caps` 只能列本实例已接线的能力，P2-02/03/07 等后续工单负责执行器、分发、缓存和发布器实现。本单只交付合同、校验函数、样例与测试。

目录：§1 传输；§2 帧；§3 握手/作用域；§4 基线与背压；§5 P1 DTO；§6 作业；§7 隐私；§8 安全；§9 页面接线；§10 命令/权限/取消；§11 查询/主题/分页；§12 敏感值精确例外；§13 P3 内部握手；§14 启动失败映射；§15 结果适配；§16 DTO 字典；§17 作废与改名。

## 1. 传输、分帧与容量

外壳按 D1 建立单实例、仅当前用户可用、拒绝远程客户端的私有命名管道，自行打开客户端一端，并仅把该客户端句柄继承给侧车。侧车启动后撤销句柄继承。页面只通过外壳的 `invoke('rt', …)` 和一条有序 Channel 通信；没有 localhost 端口。管道字节流为 NDJSON：**一行是一个 UTF-8 JSON 对象**，以单个 LF (`0x0a`) 结束；字符串中的换行必须 JSON 转义。发送端不得发送 BOM，接收端严格解码 UTF-8。

解析硬上限为 **4 MiB/帧**（含终止 LF）；常规发送预算为 **256 KiB/帧**。后者用于合批和分页决策，不能把 4 MiB 当作日常目标。大列表按稳定快照分页，导出只传受控引用号；P1 不周期推全量节点表。达到预算时优先送关键边沿与作业终态，状态主题合并为最新值，日志按 §4 计数丢弃。正常发布时，一个 100–250 ms tick 最多形成一个 `batch` 帧，`items` 可同时含多个主题及事件；不得每个主题单独 `WriteFile`。首次订阅快照和补偿快照可使用单独 `evt`，不改变周期发布规则。

接收端按字节限制读取，不能先无限缓存再检查。非法 UTF-8、JSON 语法错误、顶层非对象、字段缺失或类型/枚举错误：丢弃该帧，记带错误码的诊断；如能可信识别请求 `id` 且已完成握手，可回 `error`，不得执行命令。超出硬上限：继续有界地丢弃至下一个 LF，记 `frame_too_large`，不得解析截断内容。握手帧无效或协议不一致时拒绝握手并关闭连接；已握手连接的孤立坏帧不导致运行时退出。来自外壳的未知 `epoch` 帧一律拒绝，不沿用旧请求身份。

发布者只把紧凑事件记录到有界队列；生产者不唤醒桥接线程、不直接写管道。桥接 I/O tick 不持有 manager/route 长锁，不做运行时聚合。管道停读时不得持 GIL 阻塞；overlapped 写入的 `ERROR_IO_PENDING` 表示已提交，不能重发同一帧。`publisher_thread_cpu_ns`、tick 计划/实际时间与 lateness、生产至发送延迟、队列深度和丢弃计数是诊断指标，线程 CPU 时间不得称作“总 GIL 时间”。

## 2. 公共字段和帧类型

`proto` 固定为整数 `1`。**每个顶层帧，以及 `batch.items` 中的每个完整 `evt`，都必须有**：

| 字段 | 类型 | 规则 |
|---|---|---|
| `t` | 字符串枚举 | 帧类型。 |
| `epoch` | 非空字符串 | 侧车每次启动产生一个新的不透明启动号；首帧 `hello` 创建它，外壳回显，后续全部帧必须匹配。不能靠 PID 单独判同一代。 |
| `produced_at_ms` | 非负整数 | 业务事实产生时的墙钟纪元毫秒。 |
| `sent_at_ms` | 非负整数 | 实际提交管道写入时的墙钟纪元毫秒；背压排队期间不得提前盖戳。 |

两个时间只用于观察排队和新鲜度，不用来续期、仲裁或重新授予预算；墙钟可调整，接收端也不以 `sent_at_ms >= produced_at_ms` 判有效性。所有作业截止时间只由 Python 的 monotonic 时钟执行，**不在线上传递 monotonic 绝对值**。确需展示剩余时间时传 `remaining_ms`，它仍不能让页面延长原期限。

| `t` | 方向 | 除公共字段外的必填字段及语义 |
|---|---|---|
| `hello` | 侧车 → 外壳 | `proto:int=1`、`app_version:str`、`runtime_pid:int`、`lang:str`、`caps`、`scope`、`network_recovery`。RT-1 在构造运行时之前完成；hello 在构造之后发出，与 `bridge_main.py` 实现一致。 |
| `hello_ack` | 外壳 → 侧车 | `proto:int`、`accepted:bool`；拒绝时有结构化 `error`。必须精确版本匹配；不协商降级、不猜可选字段语义。 |
| `req` | 外壳 → 侧车 | `id:正整数`、`cmd:str`、`args:object`、`from:'page'|'shell'`；可带 `req_id:str` 和 `owner:str`。`id` 由外壳分配，在该 epoch 内唯一。 |
| `ack` | 侧车 → 外壳 | 回显 `id`、`accepted:bool`；接受作业时带 `op:str`，拒绝时带 `skipped_reason` 或 `error`。可回显 `req_id`、`owner`。它只表示接单/停止意图结果，不表示工作完成。 |
| `res` | 侧车 → 外壳 | 回显 `id`、`ok:bool`；成功有 `data:object`，失败有 `error:object`，二者互斥。P1 不把长作业成功伪装成即时 `res`。 |
| `evt` | 侧车 → 外壳 | 单个主题/作业事件；见 §4。作业事件是 `topic:'jobs'` 的 `evt`，没有单独 job 帧。 |
| `batch` | 侧车 → 外壳 | `tick_seq:正整数`（本 epoch 递增）、`items:[完整 evt,…]`（非空且每项 epoch 与外层相同）；同一 tick 的状态和边沿按产生顺序批量发送。 |
| `subscribe` | 外壳 → 侧车 | `id:正整数`、`topics:[v1 精确主题名,…]`（非空且不重复）；只可请求 `hello.caps.topics` 中的主题。独立控制消息，不占业务 `req.cmd` 白名单。 |
| `subscribed` | 侧车 → 外壳 | 回显 `id` 和完整 `topics`；必须与请求列表相同，表示订阅已生效，不表示首批快照已送达。不可部分接受；拒绝时改回同 `id` 的 `error`。 |
| `snapshot_req` | 外壳 → 侧车 | `id:正整数`、`topic:v1 快照主题`；P1 六个快照主题不带 key，v1 keyed 扩展见 §11。只可请求已订阅且已声明能力的主题。 |
| `snapshot_res` | 侧车 → 外壳 | 回显 `id`、`topic`，二选一：`snapshot` 为同 epoch、同 topic 的完整 `evt(kind='snapshot')`；或结构化 `error`。不得以补丁、空对象或旧 epoch 快照冒充恢复。 |
| `cancel` | 外壳 → 侧车 | `id:正整数`、`op:str`；独立优先控制域，由同 `id` 的 `ack` 回复；见 §6。 |
| `owner_revoke` | 外壳 → 侧车 | `id:正整数`、`owner:str`；外壳销毁窗口代次时发送，若带 `from` 只能为 `shell`，拒绝 `page`。独立优先控制域，不占业务白名单；先撤销执行器 owner，再顺序调用撤销钩子，最后回同 `id` 的 `ack{accepted:true}`，不分配 `op`。 |
| `error` | 双向 | `error:{code,message_key,params,field?,hold?,detail?}`，可带可识别的请求 `id`。只用于协议/命令错误，不代替作业终态。 |
| `ping` / `pong` | 外壳 ↔ 侧车 | `n:非负整数`，`pong` 回显 `n`；2 s 一次，读循环不经过 manager 锁。 |
| `fatal` | 侧车 → 外壳 | `where:str`、结构化 `error`；发后非零退出，并尝试既定收尾。 |
| `bye` | 侧车 → 外壳 | `exit_code:int`、`network_recovery`；表示收尾报告，不把未知残留说成恢复成功。 |

页面调用 `invoke('rt', {req_id,cmd,args,owner?})` 前须登记 Promise。外壳只返回“已入外壳队列”或即时拒绝，`ack`、`res`、`jobs` 与主题消息均经同一条有序 Channel 返回。页面收到陌生 `op` 时可缓存 5 s 等 `ack`；新 `hello.epoch` 触发 `rt:reset`：外壳清空上代缓存，页面使旧 Promise 以 `runtime_gone` 结束并从外壳缓存重订阅。外壳在 `hello_ack.accepted=true` 后显式发送一次 `subscribe`；握手本身**不**自动订阅任何主题。页面只接受同代结果；`owner` 可用于丢弃抽屉/会话关闭后的迟到结果。

## 3. 握手、能力与网络恢复

`hello.caps` 是 `{commands:[str,…],topics:[str,…],features:[str,…]}`。P1 命令只有 `service.start`、`service.stop`、`service.restart`、`app.quit`，另有独立 `cancel` 帧；可用主题只列本实例确实能发布的主题。v1（T-P2-31b）：`commands` 列侧车已登记、能执行的全部命令与查询（查询与命令共用 `req.cmd`，故同列；`host.*` 只在外壳执行，侧车从不列入）；`topics` 列本实例有 owner 发布的全部主题，含外壳专用主题。`subscribe` 只能取 `caps.topics` 的子集，含未声明主题的订阅整条回同 `id` 的 `error{code:not_allowed}`，不部分接受。`app.info.caps` 与 `hello.caps.commands` 相同。`features` 只声明后端能力。原型专有功能必须同时满足 `caps.features` 和前端构建中的用户放行白名单 `ENABLED_H_FEATURES`，外壳才开放其命令；单有后端能力不自动上线。能力可以缺席，缺席时页面隐藏或禁用相应控件，不能推断存在。

`hello.scope` 为 `{kind:'default'|'audit'|'custom',id:str}`。`audit`/`custom` 只用于开发或审计实例，不构成网络隔离，P1 **禁止其接管宿主网络**。作用域不能替代外壳进程所有权或 RT-1 网络所有权判断。

`network_recovery` 在 `hello` 和 `bye` 中分别报告 `proxy`、`firewall`：每域为 `{state,error_code,remaining_count}`，其中 `state` 仅可为 `not_needed | restored | not_owned | permission_required | failed | unknown`，`error_code` 是代码或 `null`，`remaining_count` 是非负整数或 `null`（未知）；`state='unknown'` 时 `remaining_count` 必须是 `null`。代理注册表读不到必须报 `unknown`，不能报 `not_needed`；被其他进程接管报 `not_owned`，不得覆盖；防火墙清理须独立于当前熔断开关。只有核验后才能称 `restored` 并清除恢复记录。TUN、路由和 DNS 的观测只进受控诊断，不得在这两个恢复域里伪报成功，也不得做全局重置。不自动提权；此报告也不能掩盖代理“改设置前尚未写 journal”的 B-39 缺口。

完整退出的顺序（T-P2-31b，决定_第五波裁定 31b 第 2、11 条追认）：拒收新工作 → 有序停止服务 → 关闭各业务 owner → 网络恢复报告 → 关闭日志、流量与采样生产者并做最后一次日志 flush → 发出排空 → `bye`。不变量：生产者在 `bye` 之前关闭，最后一次 flush 在 `bye` 之前，`bye` 是最后一帧。关闭业务 owner 这一步有放弃上限：某个 owner 不在预算内结束时，侧车放弃等待它（它的迟到结果一律丢弃，不进入任何主题或答复），退出照常走完网络恢复报告与 `bye`；会话结束（`app.sessionEnd`）开始后这个上限更短。上限的具体数值不属于合同（T-P2-H2 登记，见 §17）。

## 4. 主题、修订号、快照及背压

`evt` 除公共字段外必须有 `topic:str`、`rev:正整数`（该主题首发为 1）、`kind:'snapshot'|'patch'|'edge'|'gap'`、`data:object`；边沿和 gap 必须带 `seq:正整数`，补丁必须带 `base_rev:非负整数`。P1 主题全部无 key，出现 `key` 必须拒绝。`rev` 在同一个 `(epoch,topic)` 内单调递增，属于**传输发布版本**。它与运行时的 `runtime_generation`、`lifecycle_revision`、`publication_revision`、`route_epoch`、`network_context_epoch`、Core 代次和请求属主各自独立；这些归属值仍必须留在 payload 中，不能仅凭较新的 `rev` 接受旧代迟到事实。

状态主题的 `snapshot` 是完整投影；接收端以 `(epoch,topic)` 缓存最新快照。`patch` 通用形状为 `base_rev` 加 `data={changed:[{id,…},…],removed:[id,…]}`，`changed` 每项是该 id 的**完整脱敏记录**，按 id 整条替换，不作字段级深合并；两表内部不得重复 id，也不得同一 id 同时修改和删除。只有接收端缓存的 `rev == base_rev` 才能合并并升为补丁的 `rev`；不匹配返回 `snapshot_required`，用 `snapshot_req` 请求替代快照，**不得 last-wins 覆写**。参考函数 `apply_patch` 的输入为基线 `{rev,items:[{id,…},…]}` 和补丁 `{base_rev,rev,changed:[{id,…},…],removed:[id,…]}`，返回 `(merged,needs_snapshot)`：成功时 `needs_snapshot=false`，失配时 `(None,true)`；不会修改入参。**P1 没有具备 `items/id` 基线的主题，故所有 P1 `evt(kind='patch')` 均非法**；参考函数仅供后续定义增量主题时复用。新增主题必须先定义完整基线及合法 topic/kind，才能在线上启用补丁。

| P1 主题 | 合法 `kind` | 基线/序号规则 |
|---|---|---|
| `service.state`、`route.published`、`tiers.summary`、`core.info` | `snapshot`、`edge`、`gap` | `snapshot/edge` 均为完整 DTO；`edge/gap` 带 seq。 |
| `traffic.tick`、`traffic.history` | `snapshot` | 完整 DTO；无 seq。 |
| `logs.batch` | `edge`、`gap` | 记录批次为完整 DTO；两种 kind 均带 seq。 |
| `jobs` | 仅 `edge` | 完整作业 DTO，必须带连续 seq；不允许 snapshot、patch 或 gap，终态不能被吞掉。 |

状态更新在发送队列中每主题只保留最新值。必须保留的边沿包括停止意图、关键启动阶段、失败/停止完成、Core 意外退出、readiness 丧失/恢复或验证开始/结束、发布路由变化和作业终态；`kind:'edge'` 必须有连续 `seq`，其 `data` 仍为该 `topic` 的完整 P1 DTO，不能用任意局部对象绕过主题校验。边沿先记录到有界缓存，下一个统一 tick 优先发送。非作业边沿缓存溢出时发 `kind:'gap'`，其 `data={from_seq,to_seq,dropped}`，表示闭区间内全部丢失的边沿；`from_seq/to_seq/dropped` 均为正整数，`to_seq >= from_seq`、`dropped=to_seq-from_seq+1`，gap 帧自己的 `seq=to_seq+1`。`logs.batch` 自身记录也带 `seq`、累计丢弃数和 gap；作业终态依 §6 的接单限制保留，不能作为普通状态 last-wins 吞掉。

外壳在新 epoch 清除所有旧快照、补丁基线、边沿序号和日志尾。`subscribed` 后侧车先发当前状态主题完整快照，再接有序增量；所选 `traffic.history` 在每个 epoch 首次订阅时只向外壳发一次，页面重开只读外壳缓存，不重新深拷贝历史。`logs.batch` 从有界缓存续接，缺失序号用 `gap` 明示。`jobs` 在同一 epoch 内序号必须连续，绝不发送 `gap`；页面重连由外壳缓存补发作业事件，侧车回收之后外壳缓存是唯一来源。外壳若发现 `jobs.seq` 不连续，须将其视为协议错误，记录 `sequence_gap` 诊断并请求重建侧车会话（新 epoch）；外壳对尚未终态的作业以 `runtime_gone` 结束等待者。P1 阶段的 `snapshot_req` 只允许已订阅的 `service.state`、`route.published`、`tiers.summary`、`traffic.tick`、`traffic.history`、`core.info`；请求 history 时只返回该 epoch 已缓存的快照，不触发第二次历史深拷贝。`logs.batch` 按序号及 `gap` 恢复，`jobs` 按连续序号及外壳缓存恢复，均不能冒充状态快照。订阅请求/回复按 `id/epoch/topics` 配对，替代快照请求/回复按 `id/epoch/topic` 配对（参考 `validate_control_reply`）；成功的 `snapshot_res.snapshot` 替换接收端基线，收到它之前暂停该主题增量应用；失败则维持 stale 并呈现错误。页面订阅时先从外壳缓存取快照，再接有序增量。

采样、发布和陈旧判据分别如下；“无变化”本身不是采样停止证据：

| 主题 | 采样/产生 | 发布 | 接收端陈旧判据 |
|---|---|---|---|
| `service.state` | 启动/运行时最多 1 Hz，关键变化即时记录 | 运行/启动中约 1 Hz 快照及关键 edge；停止后变化触发 | 运行/启动中超过 3 s 无新快照标 stale；停止后仅看 epoch/归属及已报告的 `freshness.stale`。 |
| `traffic.tick` | Core 运行时约 1 Hz | 每次新采样快照；停止时变化触发 | Core 运行时超过 3 s 无新采样标 stale；停止后不按时间老化。 |
| `tiers.summary` | 运行中最多 1 Hz | 语义 rev 变化时；停止时一次 | 不按静默时长老化；新 epoch、归属冲突或服务采样健康失效时标未知/陈旧。 |
| `route.published`、`core.info` | 归属/状态变化时；worker 最多 1 Hz **只读**已发布快照，**禁止任何定时刷新和 Mihomo 读取**（方案 §0 第 17 条） | 变化触发 | 不按静默时长老化；新 epoch、失配或上游运行事实无效时标陈旧。 |
| `traffic.history` | 每 epoch 首次订阅时一次 | 一次快照 | 保留为历史样本并展示 `sampled_at_ms`，不得伪称实时。 |
| `logs.batch`、`jobs` | 事件发生时 | 有界边沿批次 | 日志用序号/gap，作业用连续序号及终态保留判断；空闲不算 stale。 |

运行中的 `service.state.freshness.sampled_at_ms` 随其 worker 重算而前进，可观察该状态采样是否停顿；`traffic.tick.t_ms` 单独反映流量采样。P1 没有逐个变化触发主题的独立采样心跳，因此 `tiers.summary` 静默时不能声称已证明其 1 Hz 采样继续，页面只能保持“上次观察值”，上游状态采样停顿时再标未知/陈旧。`ping` 正常也不能证明发布线程或这些采样仍活着。唤醒后重置 15 s `pong` 看门狗，避免睡眠误报。

## 5. P1 只读主题的数据合同

下表的 `int` 为 JSON 非负整数（另注除外），`str?`/`int?` 为相应类型或 `null`。所有字段由只读公开 DTO 投影，不把可变内部对象、完整 `status()` 或敏感配置原样塞入线协议。所有示例 fixture 都是完整 `evt` 帧，位于 `bridge_schema/fixtures/valid/`。

### 5.1 `service.state`

**来源**：RT-3 `lifecycle_status()`；变化立即记录、下个 tick 发布，运行中/启动中由 worker 约每秒重算并发布（业务值未变仍更新 `freshness.sampled_at_ms`），停止后仅变化时推。`data` 的五域为：

| 域 | 字段（类型） | 语义 |
|---|---|---|
| `intent` | `desired_running:bool`, `stop_requested:bool`, `lifecycle_revision:int`, `runtime_generation:int` | 同一意图记录复制。 |
| `runtime` | `runtime_running:bool`, `startup_phase:str`, `shutdown_cleanup_pending:bool`, `failure_code:str?`，可选扩展 `failure:Error|null` | `startup_phase` 为 `collecting | core_standby | running | stopping | failed | stopped`。P1 可省略 failure；P2 已知失败必须附 code/reason_code/message_key/params，code 与 failure_code 一致，无失败为 null；与 job 共用 §14 分类。 |
| `core` | `running:bool`, `pid:int?`, `process_generation:int`, `core_generation:int`, `cleanup_pending:bool` | Core 轻量事实；PID 停止时可为 null。 |
| `readiness` | `state:str`, `validation_active:bool`, `reason:str`, `remaining_ms:int?`, `publication_revision:int`, `lifecycle_revision:int`, `route_epoch:int`, `network_context_epoch:int` | `state` 严格为 `serving | ready | activation_hold | data_plane_failed | controller_hold | facility_hold | fail_closed`。验证中由 `validation_active` 表示；`serving` 与 `ready` 不能合并。 |
| `freshness` | `sampled_at_ms:int`, `snapshot_revision:int`, `stale:bool`, `uptime_s:int?` | `stale` 包括域归属不一致；不能把旧代 READY 接到新 Core。 |

各域应来自带归属版本的不可变小记录，组合时检查代次；不匹配时报告过渡/陈旧，不拼接假原子状态，也不嵌套持有多把锁。`remaining_ms` 是观测值，绝非可续期截止时间。示例：[topic_service_state.json](fixtures/valid/topic_service_state.json)。

### 5.2 `route.published`

**来源**：RT-4 `published_route_status()`；路由发布 revision/Core 代次变化时推。worker 最多 1 Hz 只读采样已发布快照；**桥接层不得调用任何路由刷新，也不得发起 Mihomo 读取**（刷新会发 `GET /proxies`、清掉失效的固定选择、推进 `route_epoch` 并干扰 D4 恢复与拨号后判定的 CAS；产品还承诺空闲不轮询，见方案 §0 第 17 条）。只因时间变久不判失效：归属一致时 `valid` 保持，年龄由 `observed_at_ms` 表达。`data` 必填：`publication_revision:int`、`owner:{process_pid:int,process_generation:int,core_generation:int,semantic_digest:str,route_epoch:int,network_context_epoch:int}|null`、`selected_chain:[str,…]`、`leaf:str?`、`managed_tier:'vanguard'|'sentinel'|'bastion'|'gateway'|null`、`observed_at_ms:int`、`valid:bool`、`invalid_reason:str?`、`fail_closed_confirmed:bool`。有已发布快照时 `owner` 是已加载 Core 路由快照的完整六元归属 key，不能少 route/network epoch。若旧快照失效，保留其 `owner` 并报 `valid:false`；**确实没有任何已发布快照**时才用 `owner:null,selected_chain:[],leaf:null,managed_tier:null,valid:false,invalid_reason:'no_published_route'`，以清除外壳缓存里的旧路由。`valid` 表示这份**已发布快照**仍适用；它和 `fail_closed_confirmed`、业务数据面 readiness 是不同事实。

只投影已发布状态；桥接不得自己从 HTTP 拼新路由，也不得把 `current_route_context().confirmed` 解释成数据面已验证。运行时自身监控发起的刷新仍经 `_refresh_runtime_route_snapshot_serialized` 原串行入口（这不是桥接层可以调用的权限）；不得新增会清掉失效固定选择的 fallback 组或全部 provider 读取。示例：[topic_route_published.json](fixtures/valid/topic_route_published.json)。

Bastion 叶子（11-02）：Core 以稳定名（`名字·摘要`，`tiers.tier3.verified_provider_writes`）加载 Tier3 成员时，`managed_tier:'bastion'` 的 `leaf` 与 `selected_chain` 末项给该节点自己的名字（与 `nodes.list` 的 `Node.name` 同源，页面据此标「当前」行）；只有快照一同发布的 proxy_map 能证明这个叶子正是该节点定义的稳定名时才替换，内置名（REJECT、DIRECT 等）从不替换，`fail_closed_confirmed` 不受影响。将来若要按 `leaf` 寻址 Mihomo，须经快照的 proxy_map 反查 Core 名字。

### 5.3 `tiers.summary`

**来源**：运行时精简层级/池快照及已发布 route；运行中最多 1 Hz 采集，有 `rev` 变化才推，停止时推一次。`data` 为 `carrier:'vanguard'|'sentinel'|'bastion'|'gateway'|null`、`offline_reason:str?`、`order:[tier_id,…]`（四层全集，含 `gateway`，共 4 项：默认 vanguard、sentinel、bastion、gateway，有承载层时承载层移到首位；T-P4-C1）、`degraded:bool`、`fallback:bool`、`tiers:{vanguard,sentinel,bastion,gateway}`（每层 `{enabled:bool,total:int,usable:int}`，`0 <= usable <= total`）、`pool:{usable:int,total:int}`。可用数必须由运行时按延迟策略计算，页面不得重跑原型 `usable()/recount()`。Bastion 计自动池，`pool` 计自有和订阅节点，不把 Bastion 混入。

**v1 增补（T-P4-C2 第 3、7 项，不升 proto）**：每层另有两个可省略字段，层对象之外的键仍是闭集（帧校验 `unknown:tiers.summary.tiers.<层>`）。
- `check_interval_s?:uint≥1`（K-2）：该层自动检测的间隔，就是 Manager 自己的调度值（`_tier_interval`：`tiers.<层>.check_interval`，正整数，缺省或非法为 60），与 `nodes.detail.check_interval_s` 同一配置键（注意：`nodes.detail` 的缺省表是 30/60/120/60，配置缺这个键时两处可能不同，见 T-P4-C2 报告）。缺席时页面不写「每 N 秒自动检测」的数字。
- `pending?:uint`（G33，overview R6）：该层 `nodes.list` 里 `status=pending`（待复检）的节点数，取自节点投影最近一次结果（同一运行代次才合并；运行时已停止或正在停止（`offline_reason` 为 `stopped`/`stopping`）、尚未投影、代次不符，或者这个数已超过 30 秒没有更新时省略）。节点投影只在 `nodes.list` 或 `subscriptions.list` 有订阅者时运行（按事实变化和每 10 秒重采样），所以这个数依赖节点投影在运行，可能比 `nodes.list` 晚一个采样周期；两个主题都没人订阅时 30 秒后省略，不发过期的数；它与 `total` 来自不同的事实源，不保证 `pending ≤ total`。缺席时总览不显示待复检计数。

P1 对前后快照差值只能称**观察到的层级变化**，不能保证完整权威边沿；若呈现 `sim:tier` 对应提示，必须带此语义，不宣称捕获了每次切换。示例：[topic_tiers_summary.json](fixtures/valid/topic_tiers_summary.json)。

### 5.4 `traffic.tick` 与 `traffic.history`

`traffic.tick` 在 Core 运行时约 1 Hz，从同一次监测采样得 `data={t_ms:int,up_bps:number,down_bps:number,session:{up_bytes:int,down_bytes:int},running:bool,error_code:str?}`；停止状态只在变化时更新。不能在页面用模拟速度代替采样。示例：[topic_traffic_tick.json](fixtures/valid/topic_traffic_tick.json)。

`traffic.history` 是**每个 epoch 首次订阅时一次发给外壳**的快照主题，页面重开只读外壳缓存，不在每个 tick 重发：`data={sampled_at_ms:int,source:'temporary_snapshot'|'bucketed',h24:[{t_ms:int,down_bps:number,up_bps:number,sample_count:int}],live:[{t_ms:int,down_bps:number,up_bps:number}]}`。`h24` 最多 288 个五分钟桶，`live` 最多 300 个秒级点；桶内 `down_bps/up_bps` 是平均速率，`sample_count` 是入桶采样数，对应原型 `h24[].d/u/n`，不是节点数或字节总量。B-02a 交付前，`source='temporary_snapshot'`：在普通 worker 从现有 `traffic_snapshot()` 一次性复制历史并降采样；它可能深拷贝最多 86,400 点，必须标记临时成本，不得放在 I/O tick 或每次状态采集里。交付公开分桶接口后改为 `bucketed`。示例：[topic_traffic_history.json](fixtures/valid/topic_traffic_history.json)。

**例外（P2-25）**：配置 reload 重建运行时 TrafficMonitor 时，同一 epoch 内允许对 `traffic.history` 做**一次**有界重播，快照 `data` 附加可选字段 `gap:true`，表示新旧 monitor 之间的历史不连续、会话累计按新实例归零。外壳收到后用新快照整体替换缓存，页面把缺口段显示为断开，不做插值；未附 `gap` 的快照仍按每 epoch 首次订阅一次的规则处理。

### 5.5 `logs.batch`

日志产端有界；每 100–250 ms 合批，单批最多 1,000 条。`data={from_seq:int,records:[record,…],dropped:int,dropped_total:int,gap:bool}`，其中 `record={seq:int,ts_ms:int,rx_ms:int,source:str,level:str,category:str,key:str,title:str,detail:str,raw:str,is_key_event?:bool,run_id?:str}`（与 §16 `Log` 同形）；`records[].seq` 严格递增，`dropped` 是本批新增丢弃数，`dropped_total` 是本 epoch 累计值且不得小于本批值。若 `dropped>0`，`gap` 必须为 true；独立 `kind:'gap'` 的闭区间与计数按 §4 校验。不能静默把 `from_seq` 跳过。`level` 为 `debug | info | warn | error | critical`，`source` 为 `TESSERACT | MIHOMO | ALERT | UNKNOWN`；category 使用 `runtime | routing | health | circuit_breaker | update | configuration | connection | alert | other`。页面订阅时外壳可给已有日志尾；过期的序号须明确 gap。

日志的 `title/detail/raw/key` 和参数在入队前脱敏：移除密码、token、私钥、完整订阅 URL、带凭据 URI、提醒通道密钥及本机敏感路径；能分类的失败只传 `reason_code`/`key`，不传原始 `suspicious_reason`。非法 Unicode 用 U+FFFD 清洗；单条无法序列化只丢该条、增加丢弃计数，不使写线程退出。面向页面的日志文本是不可信文本，不能当 HTML。示例：[topic_logs_batch.json](fixtures/valid/topic_logs_batch.json)。

**v1 增补（T-P4-C1 第 2 项，不升 proto）**：
- `key`：没有事件键的原始行在批次和历史（`logs.tail/query`）里一律是 `"log.raw"`（此前批次里是 `""`）；`source_boundary` 等其余键不变。冻结侧车仍可能发 `""`，页面把 `""` 与 `"log.raw"` 都当原始行。
- `is_key_event?:bool`：该记录的事件键在日志事件目录中的 `LogEventSpec.is_key_event`；目录里没有的键沿用采集时的显式标记；原始行为 `false`。**缺席时**（冻结侧车、旧帧）页面退回按 `key` 判断（非 `log.raw`/`""`/`source_boundary` 即视为事件行），不得把缺席当作 `false` 隐藏事件。
- `run_id?:str`：桥接内部已算的运行代号（运行开始事件生成，与 `logs.feed.run_id` 同源）；第一次运行开始之前的记录没有该字段。
- 有事件键的记录（关键事件与其他已分类事件），实时批次的 `title/detail` 是 `build_log_event_report` 按**当前界面语言**渲染的简报，与同一记录在 `logs.tail/query` 里的文字一致；语言切换（`ui.language`）后新产生的记录随新语言，历史分页每次按查询时的语言重渲染；`logs.tail/query` 的续页游标绑定该语言（标题与 `text` 过滤都随语言变化），语言切换后用旧游标续页以 `snapshot_required` 拒绝，页面按 §5.6 重新取日志（修复第 1 轮 S2）。被分类为失败的行 `raw` 仍为空串，简报只用登记参数，不含原始失败文本。集成单接线 `bridge_logs.register(session)` 之前，三处都用目录源模板（zh_CN）。示例：[p4c1_topic_logs.batch_key_event.json](fixtures/v1/valid/p4c1_topic_logs.batch_key_event.json)。

### 5.6 `core.info`

Core 启停、意外退出或代次/版本变化时推；`data={running:bool,pid:int?,version:str?,generation:int,cleanup_pending:bool}`。不附 recent_logs、路径或整个 CoreRunner 状态；Core 活着不证明路由和数据面 readiness。示例：[topic_core_info.json](fixtures/valid/topic_core_info.json)。

## 6. 作业、确认、取消与结果

P1 仅开放 `service.start {}`、`service.stop {expected_generation?:int}`、`service.restart {}`、`app.quit {}`，以及 `cancel {op}`。`app.quit` 只能由外壳发起；页面如需退出走外壳的确认流程。作业事件为 `evt(topic='jobs',kind='edge',seq=…)`，`data={op:str,req_id?:str,owner?:str,name:str,state,started_at_ms:int?,progress?:object|null,result?:object|null,error?:object|null}`；`state` 只能为 `queued | running | done | failed | cancelled | superseded`。示例：[topic_jobs.json](fixtures/valid/topic_jobs.json)。状态转换为 `queued → running → done/failed/cancelled/superseded`，排队未执行可 `queued → cancelled`；终态不可再变。

| 执行域 | P1 操作 | 约束 |
|---|---|---|
| 独立优先控制域 | `service.stop`、`app.quit`、`cancel` | 不排在长启动或普通工作后。`request_stop()` 由控制 worker 调用并拿到真实 `RuntimeStopIntent`；不能在管道读循环同步调用，也不能承诺它无锁或恒定时间。完整 stop 仍可能等订阅刷新持有的 lifecycle 锁。 |
| 串行变更域 | `service.start`、`service.restart` | 整个变更事务串行，不能只把最后的 Core reload 串行。后续模式/配置/Gateway/订阅等事务也归此域，P2 才开放。 |
| 有界普通域 | P1 无页面命令 | 后续校验、预览等有界执行；不能让普通任务占满控制域。 |

`ack.accepted=true` 仅表示作业已被有界执行器接单并分配 `op`；`service.stop` 的 ack 必须反映控制 worker 实际取得的停止意图，`accepted=false` 附 `skipped_reason`，不能猜测。start/restart 应先 ack 再运行重活。终态由 `jobs` 事件给出，不能以没有 Python 异常推断成功。`queued/running/cancelled/superseded` 不带非 null 的 `result/error`；`done` 必须带下表的完整 `result` 且 `error` 为 null 或缺席；`failed` 必须带结构化 `error`，可带同命令字段齐全的部分事实 `result`，不得把它重标为 `done`。

`service.stop`、`app.quit` 和独立 `cancel{op}` 为意图确认型，允许在 ack 前取得真实意图或取消请求，ack 写入超时、换 epoch 或断管不回滚意图、不因此改报 `cancelled`，stop/quit 照常以真实 `done/failed` 收尾并保留部分事实及终态（可送达时通过 `jobs` 交付，断管时留在终态保留区），被取消作业按自身取消方式收尾；其他带副作用的 job 命令为绝对屏障型，ack 确实写出前不得调用业务回调，拿不到回执时以 `cancelled` 结束且无副作用。

| `name` | `result` 必填字段与类型 | `done` 的额外条件 |
|---|---|---|
| `service.start` | `startup_phase:str`（§5.1 阶段枚举）、`failure_code:str|null` | 阶段只可为 `collecting/core_standby/running` 且失败码为 null；前两者不代表 ready。 |
| `service.stop` | `startup_phase:str`、`shutdown_exit_code:非负整数|null`、`cleanup_pending:bool` | `startup_phase='stopped'`、退出码为 0 或 null、清理未待办；null 表示无退出码可报告。 |
| `service.restart` | `applied:bool`、`core_running:bool`、`core_cleanup_pending:bool`、`core_generation:非负整数` | `applied=true`、Core 确实运行且无待清理；适配器还必须与操作前 Core 代次核对，不能仅因未抛异常设 true。 |
| `app.quit` | 与 `service.stop` 相同 | 停机完成才结束作业；后续 `bye` 才报告侧车最终退出码和代理/防火墙恢复真相。 |

停止仍在 `stopping`、清理未完成或非零退出码时必须 `failed` 并保留已知部分事实；restart 未真正应用时以 `failed` 加 `applied=false` 和实际 Core 事实表示。配置/订阅/Gateway 中的“已保存但未应用”也不能误报成功；这些命令及结果形状由 P2 定义。

执行器最多接纳 **64 个未终态作业**；达到上限回 `accepted=false`、`job_capacity`，优先控制域仍可处理停止/退出。终态先有序送入外壳缓存，再在侧车中保留至少 **10 分钟或最近 256 项**，回收时先满足两者中较长的保留要求。每次接单须为将来的终态预留空间；保留容量已满时限制新接单，不丢未交付终态。接单上限与终态保留共同保证同一 epoch 的 `jobs.seq` 连续，任何作业事件（包括终态）都不能丢弃或改用 `gap`。外壳在同 epoch 的页面重连时补发已缓存作业事件；侧车回收后只有外壳缓存可补发。新 epoch 清理旧作业并以 `runtime_gone` 结束等待者；同 epoch 的作业序号失连续时按 §4 的协议错误处理。

取消能力按对象声明，不存在“取消 Future 即作业已取消”的通用承诺：排队作业仅在确认未执行时成为 `cancelled`；已运行的 start/restart 由 stop 意图、代次和检查点抢占，原作业为 `superseded`；P2 的节点诊断须调用实际 `cancel_node_diagnostic(request_id)` 并等资源清理；手动订阅诊断只能用对应取消接口；出口查询当前只能撤销结果属主并忽略迟到结果，不能声称探测终止；已开始的配置事务、正式订阅刷新、Geo 和提醒测试须完成或回滚。`cancel` 的 ack 表示取消请求是否被接受，只有后续作业终态才确认实际结局。用户主动取消节点诊断——独立 `cancel{op}` 与 `nodes.cancelDiagnose` 两条路径语义统一——而临时 Core 清理失败时，`nodes.diagnose` 以 `failed` 结束，error 为 `runtime_unavailable`（`reason_code=cleanup_failed`），result 为同命令字段齐全的部分事实且 `cleanup_pending=true`；清理完成的取消仍为 `cancelled`。owner 撤销路径（窗口代次已销毁，没有观察者）维持 `cancelled`，真实清理情况记入墓碑与 `Diagnostic`。

延迟完成：除 mutation 域与 `service.stop`/`app.quit` 外，命令处理函数可交回延迟完成句柄，作业保持 `running`（res/private 请求保持未答复）但不占 worker，由属主稍后以 `complete(result)` 或 `fail(error)` 结束；结果适配、Error 采用规则、接单屏障和本命令的取消列照常生效。每个句柄有超时上限（侧车上限 900 秒，命令可设更短），超时按命令自己的超时结果结束，未定义时以 `failed`/`timeout` 结束（命令未登记 `timeout` 时为 `internal`）；换 epoch 或侧车收尾时句柄失效，等待者以 `runtime_gone` 结束；被取消、撤销或失效后属主的迟到结果一律丢弃。mutation 域与 stop/quit 交回句柄按 `internal` 失败（整个事务须占住串行域）。`exit.verify` 的延后与升级等待、`alerts.test` 等待桌面回执、`alerts.desktopDelivered` 等待写盘均以此方式等待；等待本身不占 worker，但回执的写盘是普通域的一项工作，可能排在普通工作之后，因此 `alerts.desktopDelivered` 的答复和等待该回执的 `alerts.test` 的结束都可能晚到（见 §10）。延迟中的提醒测试遇换 epoch 或退出时以 `runtime_gone` 结束；已发出的通知不回滚。

## 7. 错误与隐私

错误对象固定为 `{code:str,message_key:str,params:object,field?:str,hold?:bool,detail?:str}`。`message_key` 用稳定的 ASCII 点分 id（如 `errors.bridge.frame_too_large`、`errors.start.listener_conflict`）；页面按当前语言翻译，协议不传中文长句。`params` 只放脱敏的数值、短代码或安全显示名，其中任意层级的字符串值执行下述只记诊断的自由文本扫描。`field` 指可聚焦的表单字段；`hold=true` 表示校验拒绝或保存暂缓。`detail` 用于诊断，不直接展示，生产端仍必须脱敏；外壳日志不得记录其帧内容。

P1 bridge 错误码：`invalid_utf8 | invalid_json | not_object | frame_too_large | missing_field | invalid_type | invalid_enum | proto_mismatch | epoch_mismatch | snapshot_required | sequence_gap | not_allowed | runtime_unavailable | runtime_gone | job_capacity | internal`。运行时可透传已登记代码：`admin_required | lifecycle_busy | validation | conflict | not_running | needs_route | timeout | superseded | cancelled | io | runtime_listener_conflict | runtime_listener_unavailable | source_supply_no_egress | no_sources`；未知内部异常统一映射 `internal` 并在本地日志留诊断，不把 traceback 上线。错误码是机器语义，界面文案取 `message_key`。

### 威胁模型和防线

日志经侧车、仅当前用户/本机/单实例的私有管道、外壳到本机 WebView；各端属于同一用户、同一机器。底线是功能对等、不比旧 Qt 界面差。防护目标是配置密钥出现在界面、截图或录屏，以及密钥被外壳日志、缓存或诊断包持久化/导出。本机同用户恶意进程不属于本威胁模型：它已能直接读取配置。

**主防线是生产端入队前的已知密钥登记表精确替换。** 登记表覆盖普通文本、JSON 字符串、JSON 数值/数组、JSON 键名；按 `alerts.py` 各渠道实际使用值登记 raw 与 strip 形式（使用 raw 的 SMTP 密码、自定义 header 不擅自改空白语义），保留节点密码/UUID、订阅 URL 等配置值。常见单层百分号编码和 `\uXXXX` 编码须命中。登记表在日志路径外构建为一个长度降序的转义交替匹配器，刷新时原子交换；正式注入/配置刷新接线属于 P1-16。

**次防线是生产端特征匹配，尽力而为，固定覆盖清单：**

- Authorization 的 Basic、Bearer、Digest，以及裸 `Basic <b64>`；
- 三段 base64url JWT，首段解码成含 `alg` 的 JSON 对象，签名段可为空，载荷不要求以 `eyJ` 开头；
- Telegram、WhatsApp 上下文、Slack `xox[abcprse]-`、Discord/webhook URL、ntfy、Pushover、Gotify、SendGrid、Twilio、SMTP 等常见渠道密钥形态；
- 完整 URL/订阅 URL、带凭据 URI、PEM 私钥、用户路径；
- JSON/赋值片段中已知敏感字段名，明确列出的渠道 camelCase 名称。

生产端做单遍规范化，不反复解码；特征匹配使用预编译的合并正则。只有以 `{` 或 JSON 数组形态的 `[` 开头、或含 `":` 的文本才尝试解析 JSON（JWT 首段为特征验证而解析其已解码 JSON）。普通 `[TCP]`/`[DNS]` 日志不尝试 JSON。 默认日志合批周期为 250 ms。重复文本的纯过滤结果每个不可变登记表版本最多缓存 256 项；短原文的纯采集/分类模板最多缓存 128 项，缓存命中仍重新应用时间、级别、当前登记表和序号。缓存只在内存，登记表更新创建新的过滤缓存；无历史结果可绕过新密钥过滤。平面日志 DTO 用逐行字典复制，嵌套扩展保持深拷贝所有权。P1-13 以独立合成语料和真实采集→合批→发布→编码链路验收；即使协议不拒自由文本，生产端语料仍要求无扫描诊断。

**协议层扫描只作诊断。** 日志 DTO 精确槽位 `records[].key/title/detail/raw`、错误 `detail` 和 `params` 任意层级的字符串值，命中时只记内存诊断，允许编码发送，不拒帧、不替换文本、不计丢弃。诊断有界，仅包含固定代码 `free_text_sensitive` 和 `logs/error` 表面标签，不记录原文本、帧、提取的键名或密钥。正常 URL/赋值日志可能触发诊断，不将它们等同于已证实泄漏。其他 DTO 文本继续遵守现有校验。

**结构化敏感字段名仍拒绝。** 结构化 DTO 的 `password/token/private_key/subscription_url/url/uri/uuid` 等敏感字段、monotonic/deadline 绝对值不得上线。字段名按 ASCII 非字母数字分段，末段为 `password | passwd | pwd | secret | token | apikey | key | authorization | cookie | session | credential | credentials` 时拒绝；末段为 `count | len | length | total | size | ms | s | id` 的度量/标识符例外保留。因而 `api_key`、`metrics.token` 拒绝，`token_count/session_id` 放行。固定元数据槽位 `error.message_key` 和 `logs.batch.records[].key` 的名字仅在精确位置豁免，扩展对象中的同名键没有豁免；值仍受类型/格式校验。`traffic.tick.data.session` 仅严格 `{up_bytes:非负整数,down_bytes:非负整数}` 时豁免。结构化和自由文本提取的键使用同一分类函数，但自由文本命中仅诊断。结构化扫描保持 64 层上限，超深对象拒绝。

**个人信息是非秘密配置，不在防护目标内。** 渠道收件人、`chat_id`、手机号、邮箱地址等个人信息按 §16 ConfigValues 投影为非秘密配置，会出现在 `config.snapshot` 和设置页上（§16「个人信息提示」），截图、录屏或共享屏幕时可见；它们不进脱敏登记表（与已登记秘密相同的值除外，这种值按 §16 不投影）（决定_第五波裁定 C1-10）。

### 残余风险与交付边界

- `\xNN` 转义、Base64 编码后的登记值、多层/混合重复编码不在必达覆盖范围；单遍规范化不能承诺穷尽可还原编码。
- 未登记的 camelCase 自定义键名、无可识别特征的未知密钥可能漏过。登记表覆盖所有非空已知值，不再因 strip 后短于 6 字符而忽略有效密钥；低熵已知值可能使相同的正常文本被遮蔽。
- 配置登记遗漏、P1-16 尚未接入的初始化/刷新、刷新到生效间的窗口仍可能让未知值出现；协议层诊断不是密钥完整性证明。
- 原始侧车/旧 Qt 日志源、同用户读取配置，以及截图/录屏自身的存取控制不由本单改造。页面必须按文本节点显示不可信日志/节点名，不能拼 HTML。

**持久化/导出边界由 P3/P2 实现，本单仅规定合同：** 外壳不将原始帧落盘，只在内存缓存；外壳自身日志不记录帧内容；诊断包沿用 `diagnostic_privacy`，并额外经过登记表。本单没有实现或验收外壳落盘/导出保护。P1 没有凭据展示例外；页面不能提交任意文件路径。只有明确授权的 P2 凭据展示接口才可另定受控例外。

## 8. 边界与安全

外壳创建并持有管道所有权，侧车只信任继承的客户端句柄；不能在任意路径上监听或暴露 HTTP/WS 服务。外壳对页面 `rt` 命令执行静态白名单，P1 仅允许 start/stop/restart 和已授权的取消入口，`app.quit` 只由外壳发；没有任意 Python 方法调用、`getattr`/`setattr` 桥、任意 Mihomo HTTP 转发或页面任意路径。新建/重载窗口只能从外壳缓存和当前 epoch 重建状态；页面不能以本地 store 的模拟状态反向驱动运行时。

## 9. 原型页面对照（前端接线准则）

| v1 来源 | 原型 `T.store` / `T.sim` 对应 | P1 接线规则 |
|---|---|---|
| `hello.caps`、`scope`、恢复结果 | 原型无；新增 `caps`、`enabled`、网络恢复提示 | 两把钥匙均具备才展示原型专有操作；恢复未知/失败不能显示“已恢复”。 |
| `service.state` 快照与关键 edge | `run · phase · cancelling · uptime`；`sim:state`、`sim:phase` | 页面从五域映射旧键，新增 `failure · readiness · stale`；`uptime` 从本代启动时间/`uptime_s` 派生。`validation_active` 单独显示。 |
| `route.published` | 总览路由行、Gateway 当前路由；原型无完整权威 store 键 | 只显示已发布链和有效性；`sim:exit` 属出口身份，不由路由确认代替。 |
| `tiers.summary` | `carrier · order · fallback · degraded · tiers · pool`；`sim:tier` | 数字来自运行时；变化提示写“观察到的变化”。`T.sim.recount()/usable()` 删除。`sim:collect` 与 `collected` P2 定义。 |
| `traffic.tick` | `traffic`；`sim:tick` | `down/up` 对应 `session.down_bytes/up_bytes`，`rateDown/rateUp` 对应 `down_bps/up_bps`；仅真实运行时 1 Hz。 |
| `traffic.history` | `T.sim.history.h24/live` | 订阅时一次快照，图表保持原有两组序列；不逐 tick 深拷贝。 |
| `logs.batch` | `T.sim.logs`、`sim:log` | 按 `seq` 入日志尾，丢弃时显示 gap/dropped；页面操作日志改用运行时事件。 |
| `core.info` | `T.data.version` 的 Core 版本区域、核心状态展示 | 只读 Core 事实，不能从 `running` 推断 service ready。 |
| `jobs` 事件、`ack/res/error` | `T.ui.busy`；`restarting · cancelling`；`sim:restart` | ack 使控件进入执行反馈，终态结算；`modeBusy` 等 P2 命令尚不可接。失败按 message id 展示。 |
| `ping/pong`、`rt:reset`、主题超时 | 原型无；新增 `stale` 与重连状态 | 新 epoch 清空旧结果；看门狗和主题新鲜度分别判断。 |

`sim:mode`/store `mode`、`sim:exit`/store `exit`、`sim:collect`/store `collected`、`bastion`、`killswitch`、`update`、`latCeiling` 等映射均在 P2 的真实主题/查询交付后接线；P1 不能继续用 `sim.js` 假数据填空。原型的 `scenario()`、`setState()`、`fillTo()` 是演示行为，不作为生产命令。

## 10. 命令、白名单和执行合同

下表是闭集；未知名字、通配符、任意 Python/Mihomo 转发一律 `not_allowed`。所有查询共用 `req.cmd`，不另设 `query` 帧。P2 请求必须带 `req_id/owner`；owner 由页面生成，按连接唯一（T-P4-C3 现状，决定_第五波裁定 C3 第 1 条），外壳原样转发 owner 并覆盖页面自称的 `from`，`cancel` 只接受同一 owner 已获 ack 的 op；v1 外壳不发送 `owner_revoke`（帧与侧车撤销钩子保留，语义见下段）。`id` 是侧车管道关联号，`req_id` 是页面关联号，`owner` 是结果属主，`from` 仅作权限判定，四者不能互代。P1 四命令仍接受原有 envelope，缺少 `req_id/owner` 时保持缺席，不能用 `id/from` 填补；`app.quit {}` 等价 `reason=user`。

`owner_revoke` 只由外壳发送，必带公共 epoch/时间字段；非法帧拒收。执行器撤销先于按登记顺序调用业务撤销钩子，单个钩子异常不阻断其余钩子，全部处理后回 ack；重复撤销仍可确认。owner 是页面按连接生成的身份；若被撤销，撤销后不得复用。迟到结果和尚未提交写入的私密 res 丢弃，禁止进入公共主题、jobs 或外壳缓存；已提交的管道帧仍由外壳检查活跃 owner 并丢弃。钩子不得记录私密结果。

Python 仅执行已登记的精确命令/查询；未登记、页面调用外壳专用命令、侧车收到任何 `host.*` 均回同 `id` 的 `error{code:not_allowed}`。查询在 normal worker 执行，一次 res；普通域默认 2 个 worker、16 项队列，每 owner 上限 16，以容纳页面加载时 10 个查询和 2 个作业的突发。查询/一次性答复不产生 jobs。异步写入返回 pending 不代表 ack 已送达，只有 poll_write 完成才调用执行器 acceptance_sent。

执行域：`control` 独立优先控制域，`mutation` 完整事务串行域，`normal` 有界普通域，`sampling` 单 owner 状态域，`shell` P3 本机宿主域。原始管道读循环不执行网络、磁盘或可能持锁的动作。`host` 四命令只在外壳执行，不能转发给 Python 执行器。`audit.startup` 仅受审计环境 guard 触发，是内部动作，不是 IPC 命令。

接入层按规格表的执行域分流；`alerts.desktopDelivered` 属控制域（规格表本身，T-P2-31b 起不再有覆盖表），由接入层在接单后直接执行：它有独立的 inline 交接队列（64 项），不与 owner 撤销钩子同线程排队，执行和内存登记在接入层完成，不排在普通工作或控制 worker 的 stop 之后；inline 队列满以 `res{ok:false,error:{code:job_capacity}}` 拒绝该条回执，不是会话故障。写盘交普通域异步完成，可能排在普通工作之后；答复经 §6 延迟完成在写盘之后发出：首个回执写成答 `{recorded:true,duplicate:false}`，写失败答 `{recorded:false,duplicate:false}`；写盘进行中收到的重复回执挂到同一次写盘上，写完再答——第一次写成答 `{recorded:true,duplicate:true}`，第一次写失败答 `{recorded:false,duplicate:true}`（外壳按「已失败」处理），不存在「第一次尚未写完」的第三种含义；写盘超过 30 秒答 `ok:false`、`error{code:timeout,reason_code:receipt_persist_pending}`，外壳只记日志、不重试，侧车照常完成这次写盘；迟到/过期、未写入的回执答 `{recorded:false,duplicate:false}`；从未发出过的 ID 答 `conflict`。`recorded` 始终表示已落盘。`app.sessionEnd` 另有独立紧急交接 worker 和 1 项队列，不能等待普通入口或 owner 撤销钩子。控制／普通交接队列分别为 16／64 项；控制／普通交接队列满是 `control_capacity` 会话故障；紧急队列满（已有 sessionEnd 在排队）不是会话故障，后续 sessionEnd 以 `error{code:conflict,message_key:errors.bridge.session_end_in_progress}` 拒绝，已排队的那一条照常执行，回复也无法排出时只丢弃该回复，执行器容量满按命令答复模式报告 `job_capacity`。同一交接域按接收顺序提交，普通域 2 个 worker 可并发执行，跨域执行和 res 回复不保证全局完成顺序；res 在就绪后交付，不等待前序慢请求，同次扫描的多个就绪结果仍按登记顺序发送。job 的 ack 确认接单，jobs 的 seq 表示事实边沿顺序，均不表示请求完成顺序。

请求帧校验失败（T-P2-31b）：能可信识别 `id` 的 `req` 一律回同 `id` 的 `error`，不执行：命令名未知或页面调用外壳专用命令为 `not_allowed`，缺字段为 `missing_field`，信封枚举不符（如 `from`）为 `invalid_enum`，参数形状不符（含参数里的枚举值不符）为 `invalid_type`，其余（不变量、敏感值位置）为 `validation`；不再静默丢弃。

启动恢复门（T-P2-31a，第三波裁定方案 b）：mutation 域命令在 ack 写出之后、业务回调之前检查一次 RT-1 恢复门；门忙时作业以 `failed` 结束，result 为 null，error 为 `{code:conflict,message_key:errors.bridge.recovery_pending,params:{},reason_code:recovery_pending}`，处理函数不被调用（service.start 不记 service.state 边沿，状态没有变化）。规格表中「自带恢复门」的命令——`settings.apply` 与 `subscriptions.refresh`——由执行器放行，由处理函数自行检查并给出带部分结果的答复：settings.apply 为 hold 的 `lifecycle_busy`（`reason_code=recovery_in_progress`）并带逐节 SaveResult；subscriptions.refresh 为 `conflict`（`reason_code=recovery_pending`）并带 RefreshResult。normal 域命令、内部 mutation、stop/quit 不受恢复门影响。

有效、当前 epoch、已登记的 stop/quit 请求在接收时发布停止水位。早于水位、尚未提交的 start/restart 回 `error{code:superseded}`；交接中或等待 ack 的已接单 start/restart 按未执行作业取消，保留 ack/op 与真实终态。执行前仍检查水位，已运行请求继续依停止意图和运行时代次检查点抢占；水位之后的新 start/restart 不被旧水位淘汰。无效或旧 epoch 请求不推进水位。

答复模式：`job` 为 `ack{id,req_id,owner,accepted:true,op}` 后有序 `jobs` 终态，接单屏障先于副作用；拒绝为 `accepted:false,error` 或真实 `skipped_reason`。`res` 为一次 `res{id,req_id,owner,cmd,ok:true,data}`，失败为 `ok:false,error`；无 ack，不进入 jobs。`private` 也是一次 res，但只送给活跃请求 owner，禁止公共缓存和作业结果。HTTP/文件型 res 仍在 worker 等待，只有使用 §6 延迟完成的命令在等待期间不占 worker；“轻查询”不等于管道线程同步执行。外壳域 res 由外壳生成，内部请求 owner 用壳实例 ID。

上述绝对接单屏障适用于其他带副作用的 job 命令，`service.stop`、`app.quit` 与独立 `cancel{op}` 属于意图确认型，ack 前允许取得真实意图或请求，ack 写入超时、换 epoch 或断管后仍按真实结果收尾，不回滚意图，也不因 ack 丢失改报 `cancelled`。

取消列：`none` 不可取消；`queued` 仅确认未开始时取消，开始后完成/回滚并报告已落盘事实；`supersede` 运行中由 stop 意图和代次检查点抢占，终态 superseded；`owner` 只撤销归属并忽略迟到结果，不能声称底层探测停止；`diagnostic` 调用真实诊断取消并等临时 Core 清理；`session` 撤销会话属主/停止后续 poll，不能声称已中止 HTTP；`dialog` 原生对话框取消，无导出副作用。独立 `cancel{op}` 是控制帧，外壳检查 op 属主；它不授予其他 owner 的取消权。

每条命令均可返回公共码 `not_allowed/validation/conflict/runtime_unavailable/runtime_gone/job_capacity/internal`；表中再列业务码。作业已取消/抢占用 `cancelled/superseded` 终态；已写盘事务不改标 superseded。结构化参数错误用 `missing_field/invalid_type/invalid_enum`。schema 使用字段末尾 `?` 表示可省略，类型末尾 `?` 表示可为 null，`[T]` 数组、`a|b` 枚举、`=x` 单值；嵌套对象除明确写 `object` 外均为闭集，DTO 引用见 §16。`object` 只表示业务专属 JSON，受 §12 禁止与下游业务校验，不能作为任意运行时调用入口。

共 **34 条命令、8 条查询、29 个主题**。命令执行域计数：{'mutation': 9, 'control': 8, 'normal': 12, 'sampling': 1, 'shell': 4}；查询均为 normal（8）。主题由下表所列采样或状态 owner 产生，统一交发布器发送。

| 命令 | 域 / 来源 | 答复 / 取消 | args | result / data | 业务错误码 |
|---|---|---|---|---|---|
| `service.start` | mutation / page | job / supersede | `{}` | `{"startup_phase":"str","failure_code":"str?"}` | admin_required lifecycle_busy needs_route io timeout |
| `service.stop` | control / page | job / none | `{"expected_generation?":"uint"}` | `{"startup_phase":"str","shutdown_exit_code":"uint?","cleanup_pending":"bool"}` | lifecycle_busy io |
| `service.restart` | mutation / page | job / supersede | `{}` | `{"applied":"bool","core_running":"bool","core_cleanup_pending":"bool","core_generation":"uint"}` | lifecycle_busy needs_route io timeout |
| `mode.set` | mutation / page | job / queued | `{"mode":"system&#124;tun&#124;mixed"}` | `{"mode":"system&#124;tun&#124;mixed","changed":"bool","saved":"bool","restarted":"bool","applied":"bool","apply_error":"Error?"}` | admin_required conflict io |
| `exit.verify` | normal / page | job / owner | `{"trigger":"=manual"}` | `"IdentityResult"` | not_running needs_route timeout |
| `settings.apply` | mutation / page | job / queued | `{"base_rev":"uint","changes":"[ConfigChange]"}` | `"SaveResult"` | validation conflict io lifecycle_busy |
| `settings.validate` | normal / page | res / owner | `{"section":"str","field":"str","value":"json"}` | `{"valid":"bool","errors":"[Error]"}` | validation |
| `nodes.diagnose` | normal / page | job / diagnostic | `{"node_id":"str","probe_mode":"direct&#124;chain","request_id":"str"}` | `{"request_id":"str","status_applied":"bool","reason_code":"str?","cleanup_pending":"bool"}` | not_allowed not_running timeout |
| `nodes.cancelDiagnose` | control / page | res / none | `{"request_id":"str"}` | `{"requested":"bool","cleanup_pending":"bool"}` | conflict |
| `nodes.update` | mutation / page | job / queued | `{"item_id":"str","updates":"object","secrets?":"[SecretEdit]"}` | `"Applied"` | validation conflict io |
| `nodes.delete` | mutation / page | job / queued | `{"item_id":"str"}` | `"Applied"` | conflict io |
| `nodes.reveal` | normal / page | private / owner | `{"item_id":"str","fields?":"[str]"}` | `{"item_id":"str","fields":{"password?":"text","uuid?":"text","private_key?":"text","uri?":"text","subscription_url?":"text"}}` | not_allowed conflict |
| `subscriptions.refresh` | mutation / page | job / queued | `{"item_id":"str"}` | `"RefreshResult"` | io timeout conflict |
| `import.preview` | normal / page | res / owner | `{"tier":"tier","text":"text"}` | `{"plan_id":"str","expires_at_ms":"uint","accepted":"uint","rejected":"[Error]","groups?":"[ImportGroup]"}`；`groups` 见 §15（T-P4-C2） | validation |
| `import.apply` | mutation / page | job / queued | `{"tier":"tier","mode":"paste&#124;manual&#124;chain&#124;subscriptions","plan_id?":"str","manual?":"object","chain?":"[object]","urls?":"[text]"}` | `{"saved":"bool","applied":"bool","items":"[str]","subscriptions":"[RefreshResult]","apply_error":"Error?"}` | validation conflict io |
| `gateway.select` | mutation / page | job / queued | `{"node_id?":"str","proxy_name?":"text"}`，两者恰给一个：同时给出答 `invalid_type`，都不给答 `missing_field`。**`node_id` 优先**（T-P2-H5）：取 Gateway 行（`nodes.list` key=gateway）的 `Node.id`，侧车按渲染器的命名规则（层级顺序、`-2` 去重、空名兜底）找到该节点此刻的代理名，并按节点身份保存选择（v1 摘要：协议、地址、端口、凭据、传输参数、HTTP/SOCKS5 用户名与 gRPC 服务名；不含名字，代理名只作标签一起保存）；此后每次渲染都按身份挑 `EXIT-SELECT` 的成员，不受延迟排序、别的层级的同名节点或 Tier3 命名切换影响，身份不在导出里时 `EXIT-SELECT` 为 `[REJECT]`，绝不换成别的节点；id 不存在或已过期答 conflict/`unknown_node`，不在 Gateway 层答 conflict/`not_gateway_node`，该 Gateway 节点当前不在 Core 导出里（没有代理名）、代理名带首尾空白（运行时保存前会去空白，Mihomo 里就没有这个成员）、或同一个 id 背后的运行时节点带着不止一个身份时答 conflict/`node_unusable`，都不写入；Core 在运行时，严格 reload 确认后运行时把 `EXIT-SELECT` PUT 到已加载合同为该身份选定的成员（不是标签），`applied` 要求 PUT 回显或读回等于这个成员，否则（包括 reload 后该节点已不在导出里）答 io/`selection_unconfirmed`、`selection` 为 null；结果的 `selected` 和与之相同的 `selection` 是这个成员的显示名，与 `nodes.list` 的 `Node.name` 同规则（display_filter 加 UUID 掩码；名字不含 UUID 形状的文本时与 `gateway.current.selected` 相同）。`proxy_name` 保留作兼容：只有这个名字在当前导出里可证明只指一个 Gateway 节点时才连同身份保存，否则只存名字（渲染时按更严的名字规则，有歧义就 fail closed）；`""` = 停用 Gateway（T-P4-C2，§15） | `{"selected":"text","applied":"bool","apply_error":"Error?","selection":"str?","pending":"PendingApply?"}` | validation conflict io needs_route |
| `geo.update` | normal / page | job / queued | `{"force":"true"}` | `{"triggered":"bool","completed":"bool","status":"Geo"}` | needs_route io timeout |
| `app.update.check` | normal / page | job / queued | `{}` | `"Update"` | io timeout |
| `app.update.install` | normal / page | job / queued | `{}` | `{"verified":"bool","handoff_pending":"bool","receipt_ref":"str"}` | not_allowed io conflict |
| `logs.setMihomoLevel` | normal / page | res / none | `{"level":"info&#124;debug"}` | `{"level":"info&#124;debug","applied":"bool"}` | not_running io |
| `logs.exportBundle` | normal / page | job / queued | `{"save_ref":"str"}` | `{"ref":"str","size":"uint","files":"[str]"}` | not_allowed io |
| `logs.exportFiltered` | normal / page | job / queued | `{"save_ref":"str","format":"json&#124;text&#124;csv","seqs?":"[uint]","filter?":"LogFilter"}`；页面导出「当前视图」一律用 `seqs`（页面的清空只在本地生效，`filter` 方式看不到它；决定_第五波裁定 33-#7） | `{"ref":"str","count":"uint","missing_seqs":"[uint]"}` | not_allowed io snapshot_required |
| `alerts.test` | normal / page | job / queued | `{}`（提醒重设计 10-30：设置页「预览提醒」） | `{"ok":"bool","error":"Error?","delivery_id":"str?"}` | io timeout |
| `alerts.desktopDelivered` | control / shell | res / none | `{"delivery_id":"str","outcome":"delivered&#124;failed&#124;dropped","surface":"windows&#124;in_app&#124;none"}` | `{"recorded":"bool","duplicate":"bool"}` | conflict timeout |
| `ui.presence` | sampling / shell | res / none | `{"visible":"bool","focused":"bool","page":"overview&#124;nodes&#124;settings&#124;logs&#124;alerts&#124;about&#124;none"}` | `{"recorded":"bool"}` | validation |
| `app.quit` | control / shell | job / none | `{"reason?":"user&#124;update&#124;shell_lost"}` | `{"startup_phase":"str","shutdown_exit_code":"uint?","cleanup_pending":"bool"}` | lifecycle_busy io |
| `shell.saveGrant` | control / shell | res / none | `{"ref":"str","purpose":"bundle&#124;filtered","expires_at_ms":"uint","format":"zip&#124;json&#124;text&#124;csv","handle_id":"uint","for_owner?":"str"}` | `{"registered":"bool"}` | not_allowed conflict |
| `update.exitResponse` | control / shell | res / none | `{"handoff_id":"str","accepted":"bool","reason_code":"str?"}` | `{"recorded":"bool"}` | conflict |
| `update.installResult` | control / shell | res / none | `{"receipt_ref":"str","launched":"bool","reason_code":"str?"}` | `{"recorded":"bool"}` | conflict io |
| `app.sessionEnd` | control / shell | res / none | `{"reason":"logoff&#124;shutdown","budget_ms":"uint"}` | `{"writes_revoked":"bool","recovery":"Recovery","workers_pending":"bool"}` | io timeout |
| `host.saveFile` | shell / page | res / dialog | `{"purpose":"bundle&#124;filtered","format":"zip&#124;json&#124;text&#124;csv"}` | `{"cancelled":"bool","ref":"str?"}` | io |
| `host.openFolder` | shell / page | res / none | `{"kind":"config&#124;data&#124;logs&#124;exports"}` | `{"opened":"bool"}` | io not_allowed |
| `host.openUrl` | shell / page | res / none | `{"url":"text"}` | `{"opened":"bool"}` | not_allowed io |
| `host.reveal` | shell / page | res / none | `{"ref":"str"}` | `{"revealed":"bool"}` | not_allowed io |

### 页面与外壳白名单

**页面运行时命令（23）**：`alerts.test`、`app.update.check`、`app.update.install`、`exit.verify`、`gateway.select`、`geo.update`、`import.apply`、`import.preview`、`logs.exportBundle`、`logs.exportFiltered`、`logs.setMihomoLevel`、`mode.set`、`nodes.cancelDiagnose`、`nodes.delete`、`nodes.diagnose`、`nodes.reveal`、`nodes.update`、`service.restart`、`service.start`、`service.stop`、`settings.apply`、`settings.validate`、`subscriptions.refresh`。

**页面宿主命令（4）**：`host.openFolder`、`host.openUrl`、`host.reveal`、`host.saveFile`。

**页面查询（8）**：`app.info`、`autostart.state`、`config.snapshot`、`geo.status`、`import.specs`、`logs.query`、`logs.tail`、`nodes.detail`。

**外壳专用命令（7）**：`alerts.desktopDelivered`、`app.quit`、`app.sessionEnd`、`shell.saveGrant`、`ui.presence`、`update.exitResponse`、`update.installResult`。

**外壳专用主题（3）**：`alerts.desktop`、`shutdown`、`update.prepareExit`。页面不得订阅或请求这些快照，也不能自行声称 from=shell。validate_page_request 是页面入口 oracle，validate_response 检查响应关联；分发器仍须验证当前 epoch、活跃 owner、能力和授权 ref。形状通过不表示实际授权执行。

## 11. 查询、主题与分页基线

所有查询使用 normal 域，一次 res，来源 page（外壳可代发），取消仅撤销结果 owner；没有额外的周期查询。错误为 §10 公共码及 snapshot_required/io/timeout。静态目录按应用版本失效；其他结果按配置 rev、运行代次或水位失效，重新获取才能标为当前。查询成功不证明服务 ready 或渠道最近送达成功。

| 查询 | args | data | 新鲜度与缓存 |
|---|---|---|---|
| `app.info` | `{}` | `{"version":"str","language":"str","caps":"[str]","listeners":"[Listener]"}` | 版本/语言/能力变化失效，不含控制器 secret 或本机路径 |
| `config.snapshot` | `{}` | `{"rev":"uint","values":"ConfigValues","secret_fields_set":"[str]","read_errors":"[Error]","pending":"PendingApply?"}` | 桥接受控写 rev，外部修改不承诺 CAS，见 §15 |
| `nodes.detail` | `{"node_id":"str"}` | `{"node":"Node","config_item":"ConfigItem?","last_diagnostic":"Diagnostic?","check_interval_s":"uint","endpoint?":"Endpoint?","protocols?":"[NodeProtocol]"}`；`endpoint`/`protocols` 见 §16（T-P4-C2 第 3、5 项） | 节点 ID 与运行代次绑定，删除/换代失效 |
| `import.specs` | `{}` | `{"protocols":"[ProtocolSpec]"}` | 应用版本内静态缓存 |
| `logs.tail` | `{"since_seq?":"uint","n":"limit4000","cursor?":"str"}` | `"LogPage"` | n 1–4000；256 KiB 分页，固定 watermark/cursor，淘汰显式 gap |
| `logs.query` | `{"before_seq":"uint","filter":"LogFilter","n":"limit4000","cursor?":"str"}` | `"LogPage"` | n 1–4000；before_seq 上界、过滤器和 watermark 绑定 cursor |
| `autostart.state` | `{}` | `{"supported":"bool","enabled":"bool","target":"shell&#124;legacy_qt&#124;other&#124;none","mismatch":"bool","error":"Error?"}` | 显式查询或受控更改后重取；页面只见 target 分类 |
| `geo.status` | `{}` | `"Geo"` | 更新 owner 事实，开始/完成变化后失效 |

主题在源事实变化时记录，下一个 100–250 ms tick 合并发送；变化型主题不因空闲超时判 stale。epoch/generation 不一致、源显式 stale、gap、分页未装齐、base_rev 不匹配才标 stale；发送时刻不能代替实际 produced/observed 时间。

| 主题 | 来源域 | kinds / data | 频率与陈旧 |
|---|---|---|---|
| `service.state` | sampling | §5.1 五域 DTO | 运行/启动时约 1 秒采样，3 秒无样本或域代次不一致 stale；停止仅变化推送 |
| `route.published` | sampling | §5.2 已发布路由 DTO | 发布变化，只用 runtime 观察时间，不做路由刷新 |
| `tiers.summary` | sampling | §5.3 汇总 DTO | 变化；observed，跨代 stale |
| `traffic.tick` | monitor | §5.4 tick DTO | 运行中 1 Hz；3 秒无样本 stale，停止不误报 |
| `traffic.history` | monitor | §5.4 历史 DTO | epoch 首次种子，缺口时有界重种 |
| `logs.batch` | log_owner | §5.5 批 DTO | 默认 250 ms；seq/gap 判缺口 |
| `core.info` | sampling | §5.6 Core DTO | 进程/代次/版本变化，不能推导 readiness |
| `jobs` | executor | §6 作业 DTO | 每次状态边沿；连续 seq，断号重建 epoch，不发 gap |
| `service.collect` | sampling | edge,gap,snapshot；`{"generation":"uint","tiers":"[CollectTier]","observed_at_ms":"uint"}` | 首次检测/来源门槛事实变化，无本代证据为 unknown |
| `tier.transition` | sampling | edge,gap；`{"from":"tier","to":"tier","observed_at_ms":"uint","authority":"=observed"}` | 只报 observed，不能补造漏失的权威边沿 |
| `exit.identity` | exit_owner | edge,gap,snapshot；`{"current":"IdentityResult","previous":"IdentityResult?"}` | 请求/进度/归属变化；previous 永远标“上次结果” |
| `protocols.mix` | sampling | edge,gap,snapshot；`{"source":"loaded&#124;configured","counts":"counts","observed_at_ms":"uint"}` | 变化；按上述 owner 与基线规则判 stale |
| `network.mode` | sampling | edge,gap,snapshot；`{"mode":"system&#124;tun&#124;mixed","admin":"bool","switching":"bool","proxy_state":"owned&#124;other&#124;disabled&#124;unknown","drift":"bool"}` | 模式变化；注册表漂移最多每 5 秒观察，与路由刷新无关 |
| `killswitch.state` | rt5 | edge,gap,snapshot；`{"enabled":"bool","active":"bool","admin":"bool","error":"Error?","triggered?":"bool"}`；`active`/`triggered` = 熔断规则已安装、正在拦截（`KillSwitchManager.active`，仅在 activate 写入阻断规则后为真）；「已布防」= `enabled && admin && error==null && !triggered`；`triggered` 缺席（冻结侧车）时页面按 `active` 判断（T-P4-C1）；页面在 DTO 之上叠加运行条件（`ui/src/rt/project.js` 的 `killswitchOf`）：未启用、`error`、已触发这三种不看运行状态，其余情况下服务未运行时显示 inactive，「已布防」只在运行中成立（决定_第七波裁定 T-P4-H #2） | 变化；按上述 owner 与基线规则判 stale |
| `bastion.pool` | sampling | edge,gap,snapshot；`{"runtime_alive":"uint","user_alive":"uint","capacity":"uint","min_alive_nodes":"uint","target_alive":"uint","collecting":"bool","observed_at_ms":"uint"}`；服务停止（运行时没有 Manager）时**没有基线**：订阅后只收到 `gap`，`snapshot_req` 答 `snapshot_required`，页面按 U2 显示「未知」（决定_第五波裁定 33-#13） | 变化；按上述 owner 与基线规则判 stale |
| `bastion.sources` | config_owner | gap,patch,snapshot；`{"items":"[Source]"}` | 变化；按上述 owner 与基线规则判 stale |
| `nodes.list` | node_owner | gap,patch,snapshot；`{"items":"[Node]"}` | 变化；按上述 owner 与基线规则判 stale |
| `subscriptions.list` | node_owner | gap,patch,snapshot；`{"items":"[Subscription]"}` | 变化；按上述 owner 与基线规则判 stale |
| `gateway.current` | sampling | edge,gap,snapshot；`{"selected":"str?","effective":"str?","missing":"bool","applied":"bool","pending":"PendingApply?","selected_node_id?":"str"}`；`selected`：已发布路由有效且已加载的 `EXIT-SELECT` 持有已保存节点的成员时，是这个成员的显示名（名字随排序、同名节点、Tier3 命名移动时跟着变），否则是保存时的标签；`missing`：已加载的 `EXIT-SELECT` 不持有这个成员（节点不在导出里、身份对不上任何节点——例如凭据轮换或节点删除——或旧的只存名字的选择没有唯一的 Gateway 能承载）。**`selected_node_id?`（T-P2-H8，可省略）**：已保存的节点（身份；旧的只存名字的选择按更严的名字规则）对应的 Gateway 节点的 `Node.id`（与 `nodes.list` key=gateway 那一行的 `id` 相同，同一运行代次）。侧车只用最近一次严格 reload 的 render receipt（渲染器实际输出的代理名到节点的对应表）反解，不按名字猜：只在 Core 运行、已发布路由有效、这份 receipt 正是已发布路由所依据的那一份（同一路由权威摘要，读取前后发布修订号不变）、`selected` 在已加载的 `EXIT-SELECT` 成员里（`missing:false`）、receipt 中该名字是 Gateway 节点、且 receipt 与当前已加载的 Gateway 节点里都恰好只有一个节点带这个身份时给出，并与同一次采样的其余字段在同一组 owner 令牌之间读出（令牌含已加载导出合同即 receipt 的修订号：只换 receipt、不重新发布路由的 reload 也会让下一次采样重读，不等 30 秒复核）；停止态、无选择、`missing`、名字属于别的层、反解不到、有歧义（身份相同的多个节点）、receipt 已过期（之后又有 reload 但路由尚未重新发布、Core 或运行代次已换）时一律省略。receipt 记着渲染器为这次已保存的选择挑中的成员（选择键），所以跨层重名的 `-2` 后缀、延迟排序后后缀换位、脱敏改名、空名兜底都按 Core 实际加载的成员反解，已保存的身份还须与该成员的节点一致。旧的只存名字的选择：已发布路由正在用它挑中的成员、每个启用的 Gateway 订阅都已在运行时节点或出口缓存里到场（最近一次完成的刷新覆盖过它也算，即使它没有节点）、且没有别的出口节点（任何状态、手动或缓存；与运行时一样去重）与这个名字同族时，运行时把该成员的身份连同名字保存一次（不重载 Core，两种 Tier3 命名都适用）；同族有不止一个身份时保存为「需要重新选择」，以后节点列表怎么变都不会把它解析到幸存的那个节点上。出现时 `selected` 必为字符串、`missing` 为 false，且字段值是 24 位小写十六进制（`invariant:gateway.current.selected_node_id`）。页面有这个字段时按行高亮，没有时只以文本显示当前选择、不按名字猜行；Core 未运行（无 Core 进程、无待清理）时发布已保存的选择意图：`selected` 为已保存的选择，`effective:null`、`applied:false`、`missing:false`，不标 stale（T-P2-H2，决定_第五波裁定 33-#13），且只在 `id(config)` 与配置修订号都没变时发布，任一变化即撤下基线（gap）、等下一次采样重读（T-P2-H6）；Core 运行时 `effective` 只来自已发布路由，并要求已发布路由属于当前 Core 且配置、来源、节点秘密三个事实版本可读（运行时观察所得，任一推进即撤下基线后重读；T-P2-H6）；节点秘密版本只在已加载节点的凭据指纹变化（或 manager 对象更换）时推进，健康结果、定点校验、Tier3 准入换入凭据相同的新列表不算变化，不撤基线、不重读；凭据之外的变化靠有订阅者时每 30 秒一次的合并复核 | 变化；按上述 owner 与基线规则判 stale |
| `config.changed` | config_owner | edge,gap；`{"rev":"uint","files":"[str]","sections":"[SectionResult]","pending":"PendingApply?","external_detection":"=bridge_writes_only"}`；`files` 只列这次桥接写入本身写的文件：同一次写入引发的重载会先执行 `app_paths` 的引导与迁移，它可能另外改写 `config.yaml`、`sources.yaml`（缺少迁移标记的来源文件会被并入默认公共来源），这些不在 `files` 里，页面不能把 `files` 当作完整的改动清单（决定_第五波裁定 33-#18）；T-P2-H2 起侧车每次启动都在加载配置前执行同一引导与迁移（与 Qt 对等），这种情况一般只出现在手写或导入、尚未加载过的配置目录上 | 变化；按上述 owner 与基线规则判 stale |
| `config.items` | config_owner | gap,patch,snapshot；`{"items":"[ConfigItem]"}` | 变化；按上述 owner 与基线规则判 stale |
| `logs.feed` | log_owner | edge,gap,snapshot；`{"state":"connected&#124;reconnecting&#124;stdout&#124;stopped","level":"info&#124;debug","updated_at_ms":"uint","dropped":"DropCounts","run_id":"str?","reason?":"controller_disabled&#124;core_not_running&#124;stream_error?"}`；`reason`（T-P4-C2 第 12 项，logs R6）= 来源不在 /logs 的原因，`connected` 时为 null：先看运行时事实——Core 没在运行为 `core_not_running`，Core 在运行但没有外部控制器（`api_client` 为空）为 `controller_disabled`；再看 /logs 流自己最近的错误（连接断开或打不开为 `stream_error`）；都没有为 null。缺席（冻结侧车）时页面不区分原因 | 变化；按上述 owner 与基线规则判 stale /logs 首条事件才进入 connected，断开后使用本代 stdout；来源切换、Core 换代或停止先写 source_boundary，再同步更新状态。 |
| `alerts.history` | rt6 | edge,gap,snapshot；`{"items":"[Alert]","last_seq":"uint"}` | RT-6 序号追加，gap 后重种，禁止比较 history 长度 |
| `alerts.desktop` | alert_owner | edge,gap；`{"delivery_id":"str","event_ids":"[str]","severity":"info&#124;warn&#124;error&#124;critical","title":"text","detail":"text","members":"[str]","target":"overview&#124;alerts&#124;update","version":"str?","force?":"bool","notice?":"Notice"}`；`force=true` 表示强制测试（`alerts.test` 的桌面渠道），外壳必须走 Windows 通知、不按前台路由；侧车对每条都给出 `force`，外壳对缺少 `force` 的旧帧兼容一版 `delivery_id` 前缀 `desktop-test-` | 变化；按上述 owner 与基线规则判 stale |
| `update.state` | update_owner | edge,gap,snapshot；`"Update"` | 变化；按上述 owner 与基线规则判 stale |
| `geo.status` | geo_owner | edge,gap,snapshot；`"Geo"` | 变化；按上述 owner 与基线规则判 stale |
| `shutdown` | shutdown_owner | edge,gap,snapshot；`{"phase":"draining&#124;stopping&#124;recovering&#124;waiting_workers&#124;finished","recovery":"Recovery","workers_pending":"bool"}` | 变化；按上述 owner 与基线规则判 stale |
| `update.prepareExit` | update_owner | edge,gap；`{"handoff_id":"str","receipt_ref":"str","version":"str","verified":"true","expires_at_ms":"uint","installer_path":"str","sha256":"str","args":"[str]"}`；installer_path 为侧车数据目录内的盘符形式 Windows 绝对路径（不接受 UNC、`\\?\`/`\\.\` 前缀，无 `..`，文件名 `Tesseract-Setup-<version>-win-x64.exe`），sha256 为 64 位小写十六进制，args 为 1–16 个安装器开关，每项须匹配 `/[A-Z][A-Z0-9_]{0,31}`（不含安装器路径或任何 shell 文本）；外壳启动前须自行确认路径位于其已知的侧车数据目录内、重算 SHA-256 与 sha256 相等、并核验安装器 Authenticode（WinVerifyTrust，不联网查吊销）：接受「有效」与「无签名」，拒绝带了签名但无效或不受信任——项目正式产物不做 Authenticode 签名，完整性由 Ed25519 签名的发布清单与侧车验证过的 SHA-256 保证；任一不符即 exitResponse accepted=false | 变化；按上述 owner 与基线规则判 stale |
| `ui.language` | config_owner | edge,gap,snapshot；`{"language":"zh_CN&#124;en_US&#124;ja_JP&#124;ko_KR&#124;fr_FR&#124;de_DE&#124;es_ES&#124;pt_BR&#124;ru_RU","config_rev":"uint"}` | 变化；按上述 owner 与基线规则判 stale |

### keyed 快照、分页与 patch（P2-07 实现）

- nodes.list、subscriptions.list 的 evt/snapshot_req/snapshot_res 必须带 key=vanguard|sentinel|bastion|gateway，其余主题不得带 key。缓存主键 (epoch,topic,key)，各 key 独立 rev；订阅后分别种子四层，空层也发 items=[]。
- bastion.sources、config.items 是无 key 的 ID 列表，也允许 patch；其余主题不能 patch。基线 ID 唯一，changed 是完整替换行，removed 仅 ID，两表不得重复或相交。节点/订阅行的 tier 必须等于 envelope key。
- 超出常规预算时快照带 page={snapshot_id,index,complete,next_cursor}。index 从 0 连续，末页 complete=true/next_cursor=null；中间页必须有 cursor。所有页固定 epoch/topic/key/rev/snapshot_id，冻结同一不可变集合。
- 后续页用 snapshot_req{topic,key?,cursor} 拉取，响应回显 key；接收端先缓冲，收齐后原子安装基线。cursor 绑定 owner、查询/过滤条件、冻结水位，不是任意 offset；默认闲置 30 秒失效，每 owner 最多 4 组未完快照。
- 分页中源变化，保留有界待发增量；溢出、游标过期或 owner 撤销返回 snapshot_required，废弃整组重取，不混合两个 rev，不对半张表打 patch。validate_snapshot_pages 为冻结页组 oracle，组装成功去掉 page 元数据后再接补丁。
- 缓存 rev 必须等于 base_rev，patch rev 递增且 epoch/topic/key 相同。失配保留上次显示并标 stale，暂停增量并请求替代快照；绝不 last-wins。apply_topic_patch 不接受未装齐的分页。
- 不做周期全量节点刷新；仅首次订阅、真实缺口、明确替代请求重种。traffic.history 继续 P1 有界 live/h24 序列；B-02a 归一为最多 300/288 桶后仍走此主题，不增同名查询。日志按字节分页，4000 条只是逻辑上限；淘汰须给 gap/missing_seqs。

## 12. 敏感值的精确例外

以下是唯一允许的命令及位置，星号仅指数组索引，不是命令通配。配置读取 config.snapshot 没有秘密例外，只给安全 values 和 secret_fields_set 字段 ID，不回可写掩码；node_judgement 不下发编辑视图，保存从原配置保留。

| 命令 | 允许输入位置 | 允许私密输出位置 |
|---|---|---|
| `host.openUrl` | `args.url` | `无` |
| `import.apply` | `args.manual, args.chain, args.urls` | `无` |
| `import.preview` | `args.text` | `无` |
| `nodes.reveal` | `无` | `data.fields` |
| `nodes.update` | `args.secrets.*.value` | `无` |
| `settings.apply` | `args.changes.*.secrets.*.value` | `无` |
| `settings.validate` | `args.value` | `无` |

SecretEdit={field,action=keep|set|clear,value?}，仅 set 携带真实 value；keep/clear 不带 value，未出现的秘密保留。掩码仅展示，不能写回覆盖。settings.validate.value 只读校验例外不授予写入权；settings.apply 普通 values、nodes.update 普通 updates 禁秘密，必须走 secrets。

import.preview.text 可输入 URI；import.apply.manual/chain/urls 可含真实草稿，结果仅安全 ID/计数/错误。alerts.test.draft、配对 params/completion 允许凭据；alerts.test 带 `target_id`（config.snapshot 的渠道 ID）时测试已保存的渠道：后端取该渠道已保存的完整配置（含秘密，仅本地），draft 的值逐字段覆盖，`secrets` 是该渠道秘密字段的 SecretEdit——keep 用已保存值、set 用新值、clear 删除；没有 SecretEdit 的秘密字段沿用 draft 中的值，draft 也没有时沿用已保存值；draft 改动了标识端点或传输安全的字段（`host`、`port`、`server`、`url`、`webhook_url`、`api_url`、`ssl`、`starttls`）时，任何秘密字段都不得再取已保存值（keep 或隐式沿用），否则以 `ok:false`、`validation`、`reason_code=alerts_test_endpoint_changed` 结束，不发送；不带 `target_id` 时不得带 `secrets`；未知渠道 ID 以 `ok:false`、`conflict` 结束；pairing/draft 私密返回只给 owner，不广播。pairing 可含可信生成的 qr_svg/qr_payload/open_url，前端安全展示，不能执行未知 HTML/SVG。

nodes.reveal 仅 password/uuid/private_key/uri/subscription_url 五个输出字段，fields 参数也只允许这五项。owner 消失/抽屉关闭/epoch 改变即销毁私密结果，不能进公共缓存、jobs、外壳日志或诊断包。必须核对 epoch/id/req_id/owner/cmd 及活跃 owner，不能凭 cmd 标签认定响应授权。

新配置、草稿、测试值和配对凭据在任何可能打印的动作之前注册；事务完成前旧/新登记同时保留，之后原子换表。私密例外仍不允许结构化 monotonic/deadline 绝对值；深度、数组、帧预算照常执行。

日志自由文本按 §7/方案 §0.18：records[].key/title/detail/raw、错误 detail/params 字符串只记诊断，不拒帧、不替换、不增加 dropped；logs.tail/query 的 Log DTO 同义。结构化敏感字段名在非例外位置继续拒绝。生产端登记表脱敏是主防线。Post-dial observer / Runtime route view 已哈希 token 原样显示，不反哈希。

## 13. P3 内部握手

### 保存授权与 host

1. host.saveFile{purpose,format} 由外壳弹原生另存为并处理覆盖确认；取消返回 cancelled=true/ref=null，不启动导出。
2. 外壳打开已批准目标，通过 Windows DuplicateHandle 复制可写文件句柄到当前侧车，发仅壳 shell.saveGrant{ref,purpose,format,expires_at_ms,handle_id,for_owner}。handle_id 仅壳与侧车可见，侧车核验进程/epoch 归属、文件类型/访问权和未消费状态，不接受页面句柄。`for_owner` 是发起 host.saveFile 的页面 owner（不是外壳实例 ID），授权绑定在 (epoch, for_owner) 上；缺少 for_owner 的旧外壳沿用请求信封的 owner。外壳以自己的信封 owner `shell` 发送 saveGrant（页面 owner 被撤销时，外壳仍需要这条答复来关闭对话框流程）；侧车在 for_owner 已于本会话撤销时答 `not_allowed` 并释放句柄，不登记授权。侧车只关闭磁盘文件类型的句柄值（信任外壳传来的值；类型不是磁盘文件的陈旧值不关闭）。
3. registered=true 后才把不透明 ref 返回页面。logs.exportBundle/Filtered 的 save_ref 绑定 epoch/owner/purpose/format，默认 5 分钟过期，一次消费；页面始终拿不到路径。失败各端按所有权关句柄。
4. worker 只经授权句柄写出、flush、关闭。写入目标合同：外壳以不截断方式打开目标（OPEN_ALWAYS）；侧车写入一律从偏移 0 开始，commit 时 flush 并 SetEndOfFile 截到写入长度（覆盖较长的旧文件不留尾巴）；对已有文件执行 discard 只关闭、不截断；由本授权新建的文件在失败时由外壳删除（关闭时删除）。结果 ref 指向外壳登记文件供 host.reveal。失败/取消释放授权，不能默默写默认路径。重启后普通 ref 失效。bundle 仅 zip；filtered 仅 json/text/csv。
5. host.openFolder 仅 config/data/logs/exports 类别，受信映射目录；host.reveal 只接登记 ref。host.openUrl 仅 https、默认端口或 443、无 userinfo，主机精确为 github.com/docs.github.com/t.me/ntfy.sh/pushover.net，拒绝子域欺骗、控制字符、file/javascript 和其他 scheme。外壳确认外部链接并展示脱敏域名，不记录含凭据的完整链接。增加主机须另修合同。

### 更新退出与失败恢复

下载及签名验证成功 → 后端持久更新收据（版本/摘要/受信安装器标识，仅公开 receipt_ref） → 仅壳 update.prepareExit{handoff_id,receipt_ref,version,verified:true,expires_at_ms,installer_path,sha256,args}（字段与不变量以 §11 为准） → 外壳核验接管条件 → update.exitResponse{handoff_id,accepted,reason_code}。默认确认窗口 30 秒；拒绝、失联或超时清除 handoff 退出锁定，保留 verified 收据，不宣称安装成功。

accepted=true 只表示接管；外壳再发 app.quit{reason:update}。更新 worker 不占串行变更域等待。外壳收到 bye 后仍须分别检查 proxy/firewall 恢复和 workers，并等待侧车真实退出，之后才由外壳启动已验证安装器。bye 不等于网络全部恢复；不能用强杀掩盖清理挂起。

安装器启动失败，外壳保留实例所有权并恢复窗口；侧车已退出则起新 epoch，重跑 RT-1，服务保持停止，再发 update.installResult{receipt_ref,launched:false,reason_code}。launched=true 仅表示安装器进程已创建。此 receipt_ref 可跨受控重启，但必须解析到持久验真收据；其他普通 ref 不可跨 epoch。外壳交接日志只存 ID/版本/结果码，不存帧。

### 通知与更新版本去重

alerts.desktop 仅壳消费，delivery_id 关联真实待投递记录、事件合并成员及渠道配置版本。Windows 成功=外壳成功向 Windows 提交 toast；应用内成功=页面确认实际渲染完成。排队或收到消息不算成功。无可见表面为 dropped/none；投递失败 failed，不误标渠道配置故障。

页面渲染确认经外壳核验 delivery/owner 后转 alerts.desktopDelivered，页面不能直接发壳回执。重复回执 duplicate=true，不重复写送达。新版本仅成功回执之后记 app_update_state 最近 16 个已提醒版本；failed/dropped 下次启动重投。首次连接不重放历史；侧栏更新入口始终保留。

### 注销快路径、presence、语言

收到 Windows 会话结束消息后外壳立即发 app.sessionEnd{reason=logoff|shutdown,budget_ms}，不依赖 WebView、不排普通 app.quit。RT-2 必须有独立紧急执行机会，不能与长锁阻塞的完整 stop 共用唯一 worker。先撤权写端、再紧急恢复；相对剩余预算由后端用自身 monotonic 建原始 deadline，不跨边界传绝对值或重新授预算。超时域保留 unknown，writes_revoked=false 不能伪报成功；最终清理沿 shutdown/bye 报告。真实注销/恢复验收在 P7 专用环境。

ui.presence 仅外壳按实际 visible/focused 发，页面只提供路由页 ID；状态变化和重连发完整值。出口自动查询保留总览可见、ready、绑定稳定的旧规则，不能新增必须 focused 条件。

hello.lang 是构造后的启动语言；hello 前外壳采用自身启动语言策略。配置语言真正保存后发 ui.language{language,config_rev}（九语言），更新原生文案和页面翻译、重取需重渲染的日志，不重新执行业务或投递已回执通知。窗口关闭到托盘不是 app.quit。

### 外壳—页面合同（T-P2-31b，依 P3-03–P3-07 外壳实现）

外壳与页面之间只有 Tauri `invoke` 和一条有序 Channel（`rt_connect{channel}`），没有 localhost。以下 `host:*`、`rt:*` 帧只在外壳与页面之间流动，永远不发给侧车；外壳代页面填 `epoch`（当前 epoch，新 epoch 时随 `rt:reset` 更新）。

- **承载**：页面以 `invoke('rt', {req_id,cmd,args,owner?})` 发运行时命令、查询和 `host.*` 宿主命令。即时答复只有 `{queued:true,id}`（已入外壳队列）或即时拒绝的错误码字符串（`runtime_unavailable`、`not_allowed`、`invalid_type`、`job_capacity`、`frame_too_large` 等）；外壳组好的请求帧加约 256 字节信封余量超过 4 MiB（单帧上限）时即时拒绝 `frame_too_large`，不入队；写端若仍遇到超限帧，丢弃该帧、记 `frame_too_large`，并按帧里的 `id` 给页面回结构化 `error{code:frame_too_large}`，会话不受影响；真实结果一律从 Channel 返回：`ack`/`jobs`/`res`/`error` 帧回显 `id`、`req_id`、`owner`。`host.*` 只在外壳执行，结果是外壳自己生成的 `res{id,req_id,owner,cmd,ok,data|error}`，错误为 `{code,message_key:"errors.host.<code>",params:{},reason_code}`。外壳按 §10 白名单与 `hello.caps.commands` 放行页面命令，`cancel` 只接受同一 owner 已获 ack 的 op。
- **回放与重置**：页面每次 `rt_connect` 先收到 `rt:reset{reason:"reconnect"|"runtime_gone"}` 与当前 `hello`（T-P4-C3：两者之间是 `host:caps`，回放以 `rt:replayed` 结束，见本节末尾），再收到外壳缓存的各主题完整快照（keyed 主题按 key，分页主题为外壳已组装的完整快照）、仍陈旧槽的 `rt:stale`（见本节 `rt:stale` 一条）、本 epoch 作业事件、日志尾（被淘汰部分以 `rt:log-gap{data:{from_seq,to_seq,dropped,record_from_seq,record_to_seq,record_dropped}}` 明示）、仍有效的 `host:banner` 和最新的 `host:language`/`host:visibility`/`host:window`；之后是实时帧。新 epoch 时外壳发 `rt:reset{reason:"runtime_gone"}` 和新 `hello`，页面以 `runtime_gone` 结束旧 Promise 并清空旧缓存。外壳订阅 `hello.caps.topics` 中的全部页面主题和它声明的外壳专用主题；外壳专用主题（`alerts.desktop`、`update.prepareExit`、`shutdown`）与外壳自己请求的答复（回执、presence、saveGrant、exitResponse、installResult、外壳的 `config.snapshot`）永远不进页面 Channel；外壳不订阅 `shutdown`。`ui.language` 也由外壳消费，页面只收到由它转成的 `host:language`。外壳是 v1 列表主题（`bastion.sources`、`config.items`、按 key 的 `nodes.list`/`subscriptions.list`）的订阅方：按 §11 缓冲分页、收齐后去掉 `page` 原子安装；只在缓存 `rev == base_rev` 时合并补丁并把补丁原样转发；补丁失配、`gap` 或无 `id` 的 `error{code:snapshot_required,params:{topic,tier?}}` 时撤下该槽缓存，由外壳自己发 `snapshot_req`（续页带 `cursor`）重取，同一槽连续 3 次取不到即停止并保持 stale（两种时刻都以外壳自生成的 `rt:stale` 告诉页面，见本节 `rt:stale` 一条）；重取到的完整快照再发给页面。页面从不发 `snapshot_req`，也看不到半张表或失配补丁；列表帧的失序只导致重取，不判会话重建。
- **`host:confirmQuit{token}`**：外壳开始退出（托盘「退出」、无托盘时关窗）时询问页面，页面以 `invoke('host_confirm_quit', {token, unsaved})` 回答未保存修改数；3 秒内无有效回答时外壳直接继续退出（等同 0）。`unsaved>0` 时外壳弹原生确认框。过期或不匹配的 token 被拒（`stale_token`/`not_confirming`），迟到的回答不能重开确认。
- **`host:banner{kind,active,…}`**：外壳对侧车状态的横幅，新 epoch 保留、重连回放仍 `active` 的那条。`kind=sidecar_crashed`：`state` 为 `restarting|restarted|unrecoverable`，带 `exit_code`（可为 null）、`restarts`、`limit`；`restarted` 是一次性通知（`active:false`），替换缓存中的 `restarting`、不回放。`kind=sidecar_unresponsive`：15 秒无 `pong` 时 `active:true`、`actions:["force_restart"]`，恢复后 `active:false`、`actions:[]`。
- **`host_force_restart`**：只在 `sidecar_unresponsive` 横幅有效时可调用（否则错误 `not_unresponsive`）；外壳结束侧车进程并走崩溃重拉路径（5 分钟内最多 3 次）。
- **`host:notify{delivery_id,severity,title,detail,target}` 与回执**：外壳收到 `alerts.desktop` 后路由：`force=true`（强制测试）、窗口不在前台或页面没有声明通知能力时走 Windows 通知；否则发 `host:notify` 给页面。页面在每次连接后以 `invoke('host_notify_ready')` 声明本连接会渲染 `host:notify` 并回执；未声明（含每次重载后重新声明之前）的连接一律走 Windows 通知，因此 `alerts.desktop` 能力的公布不依赖页面实现。页面渲染完成后在 3 秒内以 `invoke('host_notify_rendered', {delivery_id})` 确认；只接受外壳交给同一页面连接、仍在等待的通知（`unknown_delivery`/`stale_page`/`already_receipted` 被拒）；3 秒内未确认记 `dropped/none`。页面从不直接发 `alerts.desktopDelivered`，回执由外壳以 shell 身份发出，每个 delivery_id 一次。`title`/`detail` 是后端按语言渲染的**不可信文本**，只能当文本节点渲染，不能当 HTML/Markdown。
- **`host:navigate{page}`**：点击 Windows 通知时外壳先请求恢复窗口，再在窗口变为可见后发 `host:navigate`；外壳会尽量在可见后再发，但不保证页面收到时窗口已经可见。`page` 来自通知的 `target`（`overview|alerts|update` 映射到页面路由）。
- **`host:visibility{visible}`**：窗口可见性变化（隐藏到托盘、恢复、最小化）时发送，外壳保留最新值并在重连时回放；页面在不可见时暂停动画与定时器。`ui.presence`（visible/focused/page）只由外壳发给侧车，页面不能伪造。
- **`host_set_page{page}`**：页面告诉外壳当前路由页，取值为 `overview|nodes|settings|logs|alerts|about|none`，只对当前页面连接有效（重载后在再次声明前视为 `none`）；外壳据此填 `ui.presence.page`。
- **`host_ready`**：页面用真实状态更新 DOM 后调用，外壳揭开（uncloak）主窗口；1.5 秒内未调用外壳也会揭开并记日志。重载页面后的调用不改变任何状态。
- **`host_window{action}` 与 `host:window{maximized}`**（11-05 页面内标题栏）：主窗口没有系统标题栏（`decorations(false)`，保留 DWM 阴影、Win11 圆角与窗外的缩放边），页面自己画窗口按钮，并以 CSS `app-region: drag` 标出标题带（WebView2 非客户区支持，wry 默认开启），拖动、双击最大化、Aero Snap 与右键系统菜单都是系统原生行为。`action` 只能是 `minimize`、`toggle_maximize`、`close`（其他值回 `invalid_args`）；`close` 等同系统关闭（Alt+F4）：照常经 `CloseRequested` 决定隐藏到托盘还是退出。`host:window{maximized}` 在主窗口尺寸变化且未最小化时发送（最小化时不发，免得还原前按钮先闪成「最大化」），值与已发出的相同时不重复发送，重连时回放最新值；页面据此显示「最大化」或「向下还原」。
- **`host:language{language}`**：外壳采用的界面语言（九语言之一）。首先来自 `hello.lang`，之后来自 `ui.language` 主题；值与已发出的相同时不重复发送，重连时回放最新值。外壳的原生文案（托盘、对话框）同步使用它。
- **`host:caps{epoch,invokes,events}`**（T-P4-C3）：每次页面连接，回放的第二帧（紧跟 `rt:reset`，在 `hello` 之前；侧车不在时紧跟 `rt:reset{runtime_gone}`）。`invokes` 是外壳实际注册的页面 invoke 名：`rt`、`rt_connect`、`rt_resync`、`host_notify_ready`、`host_notify_rendered`、`host_confirm_quit`、`host_set_page`、`host_force_restart`、`host_ready`、`host_window`（11-05；调试版另有测试钩子 `dev_quit`，不列入，页面不得调用）；`events` 是外壳自己生成的页面帧：`host:caps`、`host:confirmQuit`、`host:banner`、`host:notify`、`host:navigate`、`host:visibility`、`host:language`、`host:window`（11-05）、`rt:reset`、`rt:log-gap`、`rt:replayed`、`rt:stale`（T-P3-H2）。页面以 `T.rt.hostHas(invoke 名)` 判断外壳能力，名字一律是 invoke 名；没有收到 `host:caps` 的页面按「外壳无此能力」降级（`host_ready` 例外，照常无条件调用）。外壳的单测核对 `invokes` 与 debug、release 两张 invoke 表一致。
- **`rt:replayed{epoch}`**（T-P4-C3）：回放结束标记。每次 `rt_connect` 的回放（`rt:reset`、`host:caps`、`hello`、各主题完整快照、仍陈旧槽的 `rt:stale`、本 epoch 作业事件、日志尾与 `rt:log-gap`、有效 `host:banner`、最新 `host:language`/`host:visibility`/`host:window`）发完后发一次；侧车不在时回放只有外壳自己的状态，同样以它结束。新 epoch（外壳发 `rt:reset{runtime_gone}` 与新 `hello`）的回放就是订阅后的初始快照（seed）：外壳等 `hello.caps.topics` 声明的每个有完整基线的页面主题槽（keyed 主题每个 key 一槽；不含 `jobs`、`logs.batch` 和只有边沿的主题）都收到第一帧后发 `rt:replayed`；某槽改走重取（`snapshot_required`、补丁失配）即不再等它；最多等 15 秒（侧车在 `subscribed` 之后的下一个发布 tick 里连发该订阅的 seed 帧：先发全部状态主题快照，再发列表 seed；每 tick 以常规帧预算 256 KiB 和 32 帧为上限（本 tick 第一帧不受此限，与改前单帧相同），预算用完后下一个 tick 先让普通发布，再续发（T-P6-08）。开发机实测 29 槽在 1 秒内发完（分发版 `rt_seed_done` 约 0.67 秒），大列表多占几个 tick），到时照发并在 shell.log 记 `rt_seed_done reason=all|deadline slots missing first_missing ms`。seed 未完成时连上的页面，回放末尾不带 `rt:replayed`，由 seed 结束时补发。页面据此判定 `instant`：同一连接里 `rt:reset` 之后、`rt:replayed` 之前的帧是回放（`instant:true`，不弹边沿提示、不放动画），之后都是实时帧；不再用「每主题首帧」的启发式。`host:*` 帧不受它影响。新 epoch 的 seed 窗口（开发机实测约 0.7 秒，最长 15 秒）里到达的帧全部落在回放区间，也包括页面本连接自己发起的命令产生的作业帧和这段时间里真实发生的边沿（例如侧车重拉后立即点「启动服务」）；页面要提示自己发起的作业时，可按作业的 `req_id`/`owner` 认定，而不依赖 `rt:replayed`（是否这样做由前端决定）。seed 超过 15 秒时，迟到的 seed 首帧在 `rt:replayed` 之后到达，页面按实时帧处理。
- **`rt_resync{topic,key?}`**（T-P4-C3）：页面请求外壳把一个主题的完整快照重新经 Channel 投递（页面不能发 `snapshot_req`）。只接受 `hello.caps.topics` 已声明、有完整基线的页面主题（`jobs`、`logs.batch`、`tier.transition`、`config.changed` 与外壳专用主题都回 `not_allowed`）；`nodes.list`/`subscriptions.list` 必须带 `key∈{vanguard,sentinel,bastion,gateway}`，其余主题不得带 key（`invalid_type`）。同一 `(topic,key)` 1 秒内只受理一次，超出回 `rate_limited`（不投递、不请求）；侧车不在回 `runtime_unavailable`。成功的即时答复为 `{source}`：`cache`＝外壳缓存是当前的，已把缓存的完整快照再投递一次（状态主题的缓存若来自 `edge` 帧，投递的副本改为 `kind:"snapshot"` 并去掉 `seq`，缓存本身不变；keyed 主题只投该 key 的槽；列表主题是外壳已组装的完整快照，`kind:"snapshot"`、无 `page`；状态主题的缓存在其后收到过 `gap` 即不算当前）；`pending`＝该槽已在重取或分页组装中，那次结果会投递给页面（已用尽 3 次、只是还没被外壳丢弃的替代请求不算在途，按 `request` 重新开始）；`request`＝外壳缓存失效或没有，外壳自己发 `snapshot_req{topic,key?}`（不带 cursor），收到的完整快照再投递给页面（状态主题只接受不旧于流上已见 rev 的快照，否则丢弃并记 `stale_snapshot`：此时外壳缓存若已是当前的（流已追上），这次重取结束；否则算一次没取到，外壳再请求，同样以 3 次为限；列表主题同 §11 的槽规则，连续 3 次取不到即停，页面的下一次 `rt_resync` 可以重新开始）。侧车给 `snapshot_res` 里的替代快照分配新的 wire rev（与流共用计数，新于流上已出现的任何 rev）；该主题仍在排队、被这份快照覆盖的旧边沿与 `gap` 直接丢弃，不再上线（T-P2-H2），只有之后出现更新的边沿时，流上才会再出现该主题的值（rev 更大）；页面按 rev 去重。`cache` 重投递的帧与原帧同 rev，页面按 rev 去重/替换（等待替代快照时接受 rev ≥ 失配时已知的最大 rev）。`rt_resync` 不结束任何等待者，也不重建 Channel；页面只在传输故障（`order` 异常、Channel 关闭）重连时结束非作业等待者（决定_第四波裁定 02a-8）。
- **`rt:stale{epoch,topic,key,reason}`**（T-P3-H2，决定_第五波裁定 31b-5）：外壳告诉页面某个槽的缓存已陈旧。这是外壳自生成的事件，不带 `seq`/`rev`，不是侧车的 `gap`，外壳也不伪造带 seq 的侧车 gap。`key` 是 keyed 主题（`nodes.list`/`subscriptions.list`）的层级，其余主题为 `null`。`reason` 只有两种：`resync`＝补丁失配、`gap`、`snapshot_required`、分页组装中断（续页不连续、首帧 `index≠0`、缺 `snapshot_id`/`next_cursor`）或替代快照无效等之后外壳发出第一条替代快照请求（`snapshot_req`，不带 cursor）时，一次重取过程只发一次（在途期间的后续失配、重发未送出的请求都不再发）；`exhausted`＝同一槽连续 3 次取不到，外壳停止重取（上限 3 次，不做定时重试），之后的失配也不再发请求，同一状态只发一次。页面自己的 `rt_resync` 发起的重取不发 `resync`（页面已知），并把计数清零，重新给 3 次机会；这 3 次仍取不到时再发一次 `exhausted`（`rt_resync` 请求的状态主题同样适用）。该槽收到新的完整快照（重取成功、流上的快照或页面级状态主题的新值）后不再陈旧，外壳不另发「恢复」事件，页面以该快照为准清除标记；新 epoch（`rt:reset{runtime_gone}`）清空全部陈旧状态。页面重连时，仍陈旧的槽（包括页面自己 `rt_resync` 发起、静默记为 `resync` 的槽）在回放里（各主题快照之后、`rt:replayed` 之前）按当前 reason 再发一次。无 `id` 的 `snapshot_required` 的 `params.tier` 在 keyed 主题必须是 `vanguard`/`sentinel`/`bastion`/`gateway` 之一、其余列表主题不得带：不合法时整帧丢弃（shell.log 记 `snapshot_required_invalid_slot`），不重取、不发 `rt:stale`，页面不会收到任意 key（T-P3-H2 修复第 1 轮）。页面收到后标「数据可能过时」，提供「重新加载」按钮，触发 `rt_resync`（T-P4-06）。外壳在 shell.log 记 `rt_stale topic key reason`。
- **H 命令白名单（双钥匙，T-P4-C3）**：原型专有（H）功能的命令只有在 `hello.caps.features`（侧车已实现）与前端构建的用户放行清单 `ENABLED_H_FEATURES` 都含该功能时，外壳才放行；功能名就是清单编号（`B-31`、`B-30a`）。外壳在构建时从唯一规范路径 `shell/app/ui/build-manifest.json` 的 `h_features` 读取第二把钥匙（编号格式不对则构建失败），编进外壳；命令与功能的对应表在外壳 `hgate.rs`。v1 白名单里没有 H 命令（表为空），任何未登记的命令照旧 `not_allowed`；第一项 H 功能放行时在该单里同时登记命令、侧车 `caps.commands`/`caps.features` 和本节。
- **减少动效（`window.__TESSERACT_MOTION__`，T-P4-C3）**：外壳只读环境变量 `TESSERACT_REDUCED_MOTION`（`1`/`true`/`yes`/`on`，大小写不限，即为减少动效；其他值或未设为否），在主窗口初始化脚本（输入防护之后、页面脚本之前）注入 `window.__TESSERACT_MOTION__=Object.freeze({reduced:<bool>})`，并在 shell.log 记 `reduced_motion reduced=<bool>`。页面的 `T.motion.reduced` = 它 OR `prefers-reduced-motion`（T-P4-06）；外壳不读注册表或系统动画设置。

## 14. 启动失败映射

job 与 service.state.failure 共用分类。优先顺序：类型化 diagnostic → 权威原因 → 少量兼容文字 → 未知 internal。已知业务失败不落 internal，未知异常仍 internal，不伪造已知原因。复用已登记 code，以固定 reason_code/message_key 区分：

精确位置是 `service.state.data.runtime.failure:Error|null`，这是对 P1 的可选扩展；P2 失败适配器必须输出它，code 与同记录 failure_code 一致，reason_code 区分子原因，message_key/params 直接供页面翻译。无失败输出 null；旧 P1 可省略此字段。启动失败 job 的 error 使用同一个 Error DTO，禁止在桥接边界丢失已分类原因。

| 运行时原因 / reason_code | 协议 code | 页面 message_key |
|---|---|---|
| RuntimeListenerPreflightError | runtime_listener_conflict | errors.start.listener_conflict |
| RuntimeListenerSetupError | runtime_listener_unavailable | errors.start.listener_unavailable |
| source_supply_no_egress | source_supply_no_egress | errors.start.source_supply_no_egress |
| 确认启用来源为空且确为本次失败原因 | no_sources | errors.start.no_sources |
| no_validated_nodes | needs_route | errors.start.no_validated_nodes |
| insufficient_validated_nodes | needs_route | errors.start.insufficient_validated_nodes |
| core_missing | io | errors.start.core_missing |
| core_start_timeout | timeout | errors.start.core_start_timeout |
| core_not_running | not_running | errors.start.core_not_running |
| route_verification_failed | needs_route | errors.start.route_verification_failed |
| 兼容 fallback_warmup_failed | needs_route | errors.start.fallback_warmup_failed |
| geodata_missing（更新禁用） | validation | errors.start.geodata_missing |
| geodata_needs_route | needs_route | errors.start.geodata_needs_route |
| geodata_download_failed | io；明确超时 timeout | errors.start.geodata_download_failed |
| TUN/mixed 明确管理员要求 | admin_required | errors.start.admin_required |
| cleanup_pending | lifecycle_busy | errors.start.cleanup_pending |
| 停止意图抢占 | superseded/作业 superseded | errors.start.superseded |
| source_supply_terminal.rule_blocked | validation | errors.start.source_supply_terminal |
| source_supply_terminal.no_proxy | needs_route | errors.start.source_supply_terminal |
| source_supply_terminal.transport_failed | io；明确超时 timeout | errors.start.source_supply_terminal |
| 未知内部异常 | internal | errors.start.unexpected |

端口冲突在 `params` 中保留 kind/port；可聚焦字段只经 `Error.field` 给出，且只在保存路径（如 settings.apply 校验监听端口）出现，service.state 的失败 DTO 与启动 job 的 error 不带 field。监听建立失败不等于确定占用。节点不足只传 healthy/required 数值；no_sources 不从 nodes=0 推断，一般 PermissionError 不全映射 admin_required。兼容分类不增加重试，仅实际已安排才给重试提示。source_supply_terminal 按子原因分类，不统称 no_sources。完整路径/traceback 不上线。

## 15. 结果适配

- service.start 的 collecting/core_standby 不等于 ready。stop/quit 仍 stopping、cleanup_pending 或非零退出码即 failed，保留完整部分事实；restart 核验前后代次、实际运行/清理后才 applied=true。
- mode.set 将 system 映射 system_proxy；changed/saved/restarted/applied 都是真实事实，不能用操作前 running 当重启成功。saved=true/applied=false 是 failed，保留 apply_error。
- settings.apply 逐节保存应用，某节失败不回滚其他节，failed.result 保留 SaveResult。延后应用可 done，但仅表示保存完成：applied=false 必须伴 pending，页面显示“已保存，待应用”。Core「启动中」（`startup_phase` 为 collecting 或 core_standby）视为运行中：需要 Core 生效的保存不立即 reload，由 P2-11 的延后意图承接，页面显示「已保存，待应用」。
- 来源批量（settings.apply 的 sources 节）四点保证：①整节一次写盘（单把锁、单次 sources 文件写入）；②最多一次 reload；③任一条无效时整批 hold、什么都不写，逐条错误带 field；④写盘成功而随后的应用（reload）失败时报 `saved=true`、`applied=false` 并带 error，不回滚已写的文件；来源改值得到的新 ID 以 `id_changes` 报告。SecretEdit.clear 用在来源上返回 hold（`validation`、`hold=true`、`reason_code=source_clear_requires_delete`），要求页面删除该行而不是清空值。意图绑定 intent_id/config_rev/generation，停止/新配置/跨代后 cancelled/superseded，旧意图不暗中应用。
- 应用计划是一个 Core 方案加独立副作用；UI 偏好、自启、删除送达、延迟重检不被重启自动替代。DNS 增量失败保留重启兜底，strict reload 携 USER_GRAPH_CONFIG/user_commit_id、DNS 携 DNS_LIFECYCLE。不承诺跨文件原子回滚或只 reload 一次；延迟重检分离/来源批量公共入口后续实现。
- config.snapshot.rev 只覆盖桥接受控写入：settings.apply、mode.set、nodes.update/delete、subscriptions.refresh（已存储）、gateway.select、import.apply 每次可能改变已保存配置的写入都推进 rev 并发出 config.changed；rev 持久保存在作用域数据目录，侧车重启或运行时实例重建不归零；提交时外部文件摘要 CAS 未交付前，不承诺所有外部并发保护。隐藏 node_judgement 不可编辑，保存保留最新文件中的它；ui.theme、控制端口、来源附加键取原值，不写默认值覆盖。URL/owner-repo 互转保留附加键，秘密走 SecretEdit。
- nodes.update 复用已有事务，不重复 reload；delete 的 saved=true 表示已删除配置，应用失败仍照实报告。diagnose 的 request_id 是资源 owner，公开 ID 解析后自动池直接拒绝，reason_code=tier3_auto_pool_self_check，不调用 manager 私有方法；取消 requested 不等于临时 Core 已清理；用户主动取消（cancel{op} 或 nodes.cancelDiagnose）而清理失败时诊断作业以 failed/`cleanup_failed`、`cleanup_pending=true` 结束（§6）。nodes.update/delete/subscriptions.refresh 的 item_id 可以是 Node/Subscription ID，也可以是 config.items 的 ConfigItem ID（与运行代次无关，禁用后仍可寻址，故可重新启用节点）；同一 ConfigItem ID 对应多个条目时为 conflict，不取第一个。
- subscriptions.refresh 保留 total/healthy/stored/applied/retained_previous/apply_error/error，存储与应用不混淆。import.preview 的 plan 绑定 owner/tier/config_rev/过期时刻，apply 重验；paste 整批接受或拒绝，不承诺每节点字符位置。**`groups?`（T-P4-C2 第 13 项，G38）**：只在 `tier=vanguard` 且 `accepted>0` 时给出，把识别出的节点按物理节点分组：分组依据就是导入服务自己合并多协议条目的规则（`import_node_rules.tier1_import_entries_from_nodes`：同一地址、每行都是 URI 且协议键互不相同才合成一个物理节点），在同一次解析（`parser.analyze_content_blob`）上判定；`row_indexes` 是该组节点在识别顺序里的下标（0 至 accepted-1），每个节点恰好属于一组，单协议节点自成一组；`key` 是组的序号（`g0`、`g1`…），不由地址派生；`label` 只在合并条目的名字来自节点名（不是地址兜底）、通过帧敏感扫描，并且经过与 `Node.name` 相同的过滤（这批节点自身的秘密、UUID）后不变、不含任何识别出的节点地址时给出，是不可信文本；`key` 必须是 `g<序号>` 形状（`invariant:<路径>.key`）。与服务自己的计数（accepted、物理节点数）不一致或解析失败时省略 `groups`。逐行部分导入仍是清单 B-37，不在 v1。首次刷新失败可 saved=true，但 job failed 并保留逐项结果。
- gateway.select 的 selected 是意图，selection/effective 是实际事实；applied=false 不假装成功切换。**选择语义（T-P4-C2 第 9 项，K-10）**：`proxy_name` 是 Mihomo 里 `EXIT-SELECT` 组成员的代理名，页面取 Gateway 行（`nodes.list` key=gateway）的 `Node.name`；只有当这个名字在全部层级的节点里唯一、且未被脱敏改写时，两者才相同（重名会被 Mihomo 配置加 `-2` 后缀，名字为空时用 `协议-地址:端口`）。`gateway.select` 的 node_id 已由 T-P2-H5 落实，见 §10：选择按节点身份保存（见 §10/§11），名字只作标签；反过来，`gateway.current.selected_node_id?`（T-P2-H8）由已保存的身份经 receipt 给出 `Node.id`，见 §11。`proxy_name:""` = **停用 Gateway**：运行时删除已保存的选择、把 FINAL-EXIT 指回 PROXY-FALLBACK（`set_exit_selected_proxy("")`）；结果 `selected:""`，成功（done）时 `selection:null`、`applied:true`——Core 运行时要求严格 reload 已确认，且把 FINAL-EXIT 切到 PROXY-FALLBACK 的 PUT 已被 Mihomo 接受（依据是这次 PUT 成功后回显的目标，不是事后从 Mihomo 读回的 FINAL-EXIT 当前值；v1 没有无副作用的 FINAL-EXIT 读回），Core 未运行时要求已保存的选择为空（读不到时按运行时已完成的写入算）；其余情况与普通选择同样以 apply_error 报告、作业 failed。非空的 `proxy_name` 仍不得有首尾空白、不得含 `://`（validation）。Core 未运行时保存成功即 done、`applied=true`（下次启动生效），此时 selection 与 gateway.current.selected 为已保存的选择意图。geo.update triggered 与 completed 分开，无出口 needs_route。更新 check 失败不抹历史，install 仅 verified/handoff_pending，不代表已安装。
- alerts.test ok=false 必须 failed，桌面测试等待真实回执；带 target_id 时测试已保存渠道（合并规则见 §12）。onboarding complete 只返独立草稿，saved=false；辅助 ntfy/Pushover 二维码不能配置或启用。owner 撤销后 poll 不复活，默认十分钟过期。
- IdentityResult 保留 request/generation/binding、双栈独立状态、partial、时间、四个诊断布尔。route_contract_failed → errors.exit.route_contract_failed：所属组 Google 合同失败，中立通过且无可用备用。
- native_fallback_verified → errors.exit.native_fallback_verified：Mihomo 自行切换且已核实，应用没有写选择；native_fallback_verified_commit_rejected → errors.exit.native_fallback_verified_commit_rejected：核实后的提交被拒，不称已采纳；native_fallback_unverified_budget → errors.exit.native_fallback_unverified_budget：预算不足、未经核实；manual_execution_binding_stale → errors.exit.manual_execution_binding_stale：手动绑定已失效，不接纳迟到结果。
- exit.verify 取消仅撤归属；429/Retry-After 共用原始 deadline，previous 永远标“上次结果”。自动调度不刷新路由、不替代 Gateway 复检。tier 变化只报 observed。

## 16. 结构 DTO 字典

字段末尾 ? 表示可省略，类型末尾 ? 表示 null，[T] 数组，a|b 枚举，=x 常量；uint 拒绝 bool，数组最多 4000 项且受帧预算。object 由现有配置/目录公共校验器限定，不允许借此增加任意方法调用或绕过秘密/隐藏字段规则。Error.params 是安全数据，label_id 是稳定消息 ID，secret_set/secret_fields_set 是状态/字段 ID。

`CollectTier` 固定四层各一行，generation 与包络一致，first_check=unknown 表示无本代证据，观测为空时字段值为 null，不填零假装检测完成。`Protocol counts`（类型 counts）是协议名到非负节点数的映射，不允许字符串或负数计数；source 指明 loaded/configured。`DropCounts` 五级计数与 total 必须一致。Listener.port 单位为 TCP 端口，1–65535，available=null 为未观测，不能据此推出 readiness。Diagnostic.latency_ms 为非负毫秒，null 为没有结果，state/status_applied/cleanup_pending 分别表示探测、回写、资源清理事实。

ConfigValues 仅允许字典中列出的十节安全投影，不能直接转发整份 client/tiers 配置；节内字段沿用既有配置公共校验器定义。network 只投影界面可编辑的模式、TUN、系统代理和监听设置；dns 为 DNS 编辑项；routing 为规则/规则集安全配置；latency 为延迟策略；sources 为 Source 安全行；alerts 为渠道 ID/类型/标签/启用/事件范围及非秘密设置；ui 只给语言与用户偏好；autostart 为启用状态；traffic 为图表/保留偏好；killswitch 为用户开关与放行设置（`enabled`、`allow_lan`、`allow_dns`、`dns_whitelist`、`additional_whitelist`，T-P4-C1）；bastion 为 Bastion 策略 `{enabled:bool,min_alive_nodes:uint}`（T-P4-C1，见下文）。network 另含 `egress_recovery{enabled,retry_source_refresh}`（只这两个开关，`node_judgement` 永不投影）；ui 另含 D8 `start_in_tray_on_autostart`；alerts 渠道行的 `config` 按 `alerts.catalog` 投影该渠道**全部非秘密**字段（如 `chat_id`、`to`、`from`/`from_addr`、`host`、`port`、`tags`、`phone_number_id`、模板文本），catalog 或 `bridge_vocab` 标为秘密的字段（令牌、密钥、webhook/服务器 URL、ntfy 服务器与主题、SMTP 用户名与密码、凭据类 header）从不出现，对象值（payload、template、headers）不投影。渠道行 `alert_types` 是**生效的**级别过滤（与投递同一优先级：行上的控制字段优先于 `config.alert_types`）；两处都未设置时，有费用的渠道（`pushover`、`sms_twilio`、`email_sendgrid`、`whatsapp`）显示其默认的仅紧急级别 `["CRITICAL","EMERGENCY","KILL_SWITCH_ON"]`，其余渠道显示 `[]`；`[]` 一律表示「全部级别」，页面据此区分「仅紧急（默认）」与「全部」（修复第 1 轮 M1）。**个人信息提示**：渠道的收件人与标识（`to`/`to_addrs`/`to_numbers`、`chat_id`、`phone_number_id`、邮箱地址、`host` 等）按上条是非秘密配置，会出现在 `config.snapshot` 与设置页上，截图、录屏或共享屏幕时可见；它们不进脱敏登记表（与已登记秘密相同的值除外，见下条）。任何列表字段（DNS 解析器、熔断放行名单、渠道收件人与 `tags`、规则列表等）只要有一项不能原样投影（URL、已登记秘密如与 SMTP 用户名相同的收件地址、非标量），**整个字段**不进 values；其中含秘密项的，记入 `secret_fields_set` 的字段级 ID（如 `alerts.<渠道>.to_addrs`、`killswitch.additional_whitelist`，不是逐项的 `.<序号>`），保证原样回存不会删掉被扣下的那一项（修复第 1 轮 M1）。**页面提交这种字段时整体替换**：字段级 ID 的列表含被隐藏的项，`settings.apply` 里给出的值替换整张列表，被隐藏的项不保留、不合并；页面编辑前必须让用户明确确认「会替换整张列表」（决定_第五波裁定 C1-#5）。具有 URL/凭据的配置值不直接出现在节内：读取为 secret_fields_set 中稳定字段 ID，写入通过 SecretEdit。SecretEdit.field 使用相应协议/渠道目录登记的字段 ID，target_id 指向渠道/来源/节点配置 ID；适配器必须按 section/target_id/field 解析并校验，不允许任意点分路径写整个配置。P2-09/10/13 负责使用已有校验器落实具体字段，未知/隐藏项拒绝，未提交项从最新原文件保留。node_judgement 保留只发生在后端读写合并，不经过页面往返。

**DNS 解析器地址不是凭据（T-P4-C1 第 4 项）**：`dns` 节的 `nameserver`、`default_nameserver`、`proxy_server_nameserver`、`direct_nameserver`、`fallback` 列表里，`https://`、`tls://`、`quic://` 等解析器地址按普通配置投影（`config.snapshot`），页面也可在 `settings.apply` 的 `dns` 变更里原样提交；豁免只对这五个 DNS 字段，判定与帧校验共用 `bridge_protocol.public_dns_resolver`，**失败关闭**（T-P2-H2，决定_第五波裁定 C1-#2）：判为公开必须同时满足——没有 userinfo（`@`）；没有查询串与片段（`?`、`#`，含 Mihomo 的 `#RULES` 一类后缀）；路径为空或恰为 `/dns-query`、`/resolve`、`/query`、`/dns`；主机是 IP 字面量，或主机名中除最后两个标签（可注册域）之外的每个标签都不像随机 ID（六位以上全十六进制、六位以上且含数字、或超过 24 个字符的都算像），且主机不在已知私有解析区（`d.adguard-dns.com`、`dns.nextdns.io`、`dns.controld.com`）之下——区名左边只要还有任何标签，不论拼写一律按秘密；其余部分也不含敏感文本。拿不准的一律按秘密处理，重新进入脱敏登记表；一个列表只要含一项秘密地址，整个字段不进 values、记入 `secret_fields_set`（`dns.<字段>`），保证原样回存不会丢掉这一项（与上条所有列表的整字段规则相同）。路径或主机名里带私有 ID 的地址（NextDNS 的 `dns.nextdns.io/<id>`、`<id>.dns.nextdns.io`，AdGuard 的 `/dns-query/<id>`、`<id>.d.adguard-dns.com`、`<设备名>.<id>.d.adguard-dns.com`，ControlD 的 `<id>.dns.controld.com`）因此按秘密处理；`dns.google`、`cloudflare-dns.com/dns-query`、`1.1.1.1`、`tls://dns.alidns.com`、`dns.adguard-dns.com`、`family.adguard-dns.com` 仍为公开。这不是 §12 的私密例外：判为公开的地址不进脱敏登记表。

**Bastion 写入口（T-P4-C1 第 1 项，K-7）**：`ConfigChange.section=bastion`，`values` 为 `{enabled?:bool,min_alive_nodes?:uint}`，走旧界面 save-tier3 的同一入口（`tier3_config_view.apply_tier3_config_payload` 写 config.local.yaml 的 `tiers.tier3.enabled`/`min_alive_nodes`，经 `update_local_config` 做一次 Python 重载），不写容量、目标或任何 Tier3 调度键。`min_alive_nodes` 取 1 至运行时容量（`bastion.pool.capacity`，即 `max_pool_size`；D9；与端口等其余整数字段同一校验器口径，`settings.validate`/`settings.apply` 也接受纯十进制数字字符串如 `"7"` 并规范为整数，布尔一律拒绝），超出以 `validation`、`field=bastion.min_alive_nodes`、`hold:true` 拒绝；效果由 `settings_effects.plan()` 给出——该节不在其 Core 计划内，SectionResult 为 `effect:none`、`applied:true`（与旧界面一致，只做 Python 重载）。`config.snapshot.values.bastion` 与旧界面卡片读法相同（`min_alive_nodes` 已截到容量内）。

**分流规则一张表（配置整理 P2，11-04）**：`routing` 节另含两项。
- `rule_list`：用户规则按匹配先后排成的一张表（`routing_rules.effective_rule_list`）。
  - 来源：配置里有 `routing.rules.list` 时就是它；没有时，把旧的三组规则（`routing.rules.direct/proxy/reject`，按 `routing.rule_order`）和配置文件里的 `raw`/`rule_sets` 读成一张表，编译结果与旧格式逐条相同。
  - 单条：`{to:"direct|proxy|reject", kind, value, not?:bool, resolve?:bool, off?:bool, note?:"dropped_policy|target_gone|unreadable"}`。`kind` 为 `domain|suffix|keyword|regex|geosite|geoip|ip-cidr|ip-asn|src-ip-cidr|dst-port|src-port|process|process-path|network|rule-set`。
  - 组合：`{to, kind:"and|or", of:[条件,…]}`，2–8 个条件，条件与单条同形，但没有 `to`/`off`。
  - 页面拆不开的 Clash 规则：`{to, kind:"clash", value:"TYPE,载荷", params?:[…]}`，只能原样保留、移动或删除。
  - 编译：`to` 编译为 `DIRECT`/`GENERAL`/`REJECT`。IP 类条件（`geoip`、`ip-cidr`、`ip-asn`）默认带 `no-resolve`，`resolve:true` 时才解析域名。
  - `off:true` 的行留在表里，但不编译。这些是旧配置里本来就没生效的规则：`dropped_policy` 指向不允许的策略，`target_gone` 指向已不提供的线路，`unreadable` 无法识别。
  - 旧规则若会被下述检查拒绝（值的写法不合法，或与 Tesseract 自身规则冲突），读成 `clash` 行，编译结果不变，也不挡以后的保存。
  - 整张表只要含 `://`，整字段不进 values，`secret_fields_set` 记 `routing.rule_list`。
- `providers`：规则集行 `{name, type?, behavior?, format?, url? | url_hidden?:true}`（`routing_rules.provider_rows`）。地址只在公开时给出（`http(s)`，无 userinfo、查询串与片段），否则给 `url_hidden:true`。

**写入**：`ConfigChange.section=routing` 的 `values.rule_list`、`values.providers` 各自**整体替换**，不合并，分别写入 `routing.rules.list` 与 `routing.rule_providers`。
- 写入之后，旧的三组规则与 `raw/rule_sets` 不再参与编译：`routing.rules.list` 就是全部用户规则，空表也是。
- `providers` 中不带 `url` 的行，按名称保留已存的同名规则集原样（被隐藏的地址、本地文件）。
- 新行必须用公开地址。`behavior` 为 `domain|ipcidr|classical`；`format` 按地址后缀推断，`.mrs` 只能配 `domain`/`ipcidr`；规则集经 FINAL-EXIT 下载。

**冲突护栏**（`routing_rules.normalize_rule_list`，与 Tesseract 排在用户规则之前的系统规则一致；带 `not` 的条件不受限）：
- 只指向本机或局域网的条件只能 `direct`（`errors.settings.rule_local_only`）。
- Tesseract 自己使用的主机（出口检测、探测、规则数据下载）只能 `proxy`（`rule_reserved_host`）。
- Tesseract 自己的程序（`tesseract.exe`、`tesseract-runtime.exe`、`mihomo.exe`）一律拒绝（`rule_own_program`）。
- `rule-set` 必须指向现有规则集（`rule_unknown_set`），`off` 行除外。
- 最多 2000 条（`rule_too_many`）。
- 规则集同名（不分大小写）拒绝（`rule_set_duplicate`）；地址不合格拒绝（`rule_set_url`）；删除仍被未停用行使用的规则集拒绝（`rule_set_in_use`）。

以上都以 `validation` 拒绝，`params.field` 指向具体的行，如 `routing.rule_list[3].of[1].value`、`routing.providers[1].url`。用户规则编译后夹在 Tesseract 的系统规则（FINAL-EXIT 精确域名、本机与局域网）和 `MATCH,GENERAL` 之间，不改变它们。

**原样回存**（修复第 1 轮 M2 收窄范围）：只承诺**可编辑字段**——`bridge_validation.FIELDS` 登记的字段（嵌套块按 `NESTED`：`network.tun` 的 `enabled/stack/auto_route/strict_route/auto_detect_interface`、`network.system_proxy` 的 `enabled/bypass`、`network.egress_recovery`；`routing.rule_list` 与 `routing.providers`（配置整理 P2，整表，见上条）；旧的 `routing.rules` 的 `direct/proxy/reject/compatibility_defaults`、`routing.rule_order`（三组规则的匹配先后，`direct/proxy/reject` 的一个排列；快照总是给出补全后的完整排列，文件里没有这个键时就是默认的 `direct, proxy, reject`；新界面不再提交这两项，文件里有 `routing.rules.list` 时它们不参与编译）；`sources` 以 `{items:values.sources}` 提交）——按 `config.snapshot` 的值作为 `settings.apply` 的 changes 提交时，所有节都是 saved/applied、不写盘、配置文件逐字节不变、rev 不前进（已存渠道行显示为 `""` 的缺省 label、等于生效过滤的 `alert_types`、只在一层出现的控制字段、未改动的已存渠道字段，均按原样保留；新建的渠道行不做这种「视同未改」处理，`alert_types:[]` 照写为「全部级别」）。**只读投影字段不得回交**，提交即以 `validation`（`errors.settings.invalid_field`）拒绝：`dns` 的 `enable/listen/ipv6/enhanced_mode/fake_ip_range/fake_ip_filter`，`network` 的 `mode`（写入用 `network_mode`）/`controller`/`subscription`，`network.tun` 的 `device/mtu/dns_hijack`，`network.system_proxy` 的 `host/port/startup_timeout/health_failures_before_restore`，`routing.rules` 的 `raw/rule_sets` 与 `routing.rule_providers`，`ui.theme`，以及 `traffic` 的投影键（写入键为 `enabled/history_seconds/sample_interval_seconds`）。

**重新启用已停用节点（T-P4-C1 第 8 项，K-11）**：已停用节点不在运行节点里，没有 `Node.id`；页面用 `config.items` 的 ConfigItem 稳定 ID 作 `nodes.update{item_id, updates:{enabled:true}}`，侧车按 `bridge_runtime_access.config_item_id` 解析（T-P2-31b 起已实现）；同一 ID 对应多条重复项时以 `conflict` 拒绝。其他字段（含协议端口，B-17 附属）规则不变。

**提醒渠道的凭据请求头（T-P2-H11，决定_第九波裁定）**：①**投影**：渠道 `config.headers` 非空时照旧登记整字段 `alerts.<id>.headers`；其中每个值非空的**凭据请求头**（`bridge_vocab.alert_secret_path`：`authorization`、`proxy-authorization`、`cookie`、`x-api-key`，或名字含 `token`/`secret`/`key`，不区分大小写）另登记 `alerts.<id>.headers.<名>`，名字保留保存时的大小写，**只有名字，值从不出现**。顶层旧式块上的渠道（见 ②）不登记逐个 ID，只有整字段 ID。名字本身像秘密时不单独登记、只有整字段 ID（失败关闭）：不是「字母开头、只含字母数字与 `-` `_`、至多 64 个字符」的记号；按 `-`/`_` 切开后有一段像随机 ID（六位以上全十六进制、六位以上且含数字、或超过 24 个字符）；含已登记秘密（显示过滤会改写它）；或整条 ID 会被帧敏感扫描拒绝。普通请求头不登记名字，投影不变（对象值仍不进 values）。②**逐个清除**：`SecretEdit{field:"headers.<名>",target_id,action:"clear"}`（名字不区分大小写）写盘时从该渠道 `config.headers` **删除这个键**（同名不同大小写的键一并删除），不留空值；同一请求可清除多个请求头；`headers` 里没有这个名字时**以字段级错误拒绝**（`validation`、`errors.settings.invalid_field`、`field=alerts.secrets[<序号>].field`、`hold:true`），整节不落盘，不做幂等成功。顶层旧式块上的 clear 同样拒绝。顶层旧式块的认定与写盘一侧（`bridge_settings.write_section`）相同：行 id 是配置里真实存在的 `alerts.<类型>:` 块（`config.snapshot` 中这种块的行 id 等于类型），或是保留 id `desktop`、`telegram`、`whatsapp`、`sound` 之一；最小差异写入无法删除基础配置与本地配置合并后的键，这种渠道改用 set，或删除后重建。`alerts.channels` 列表里 id 恰好等于类型的渠道（Qt 保存列表渠道时默认就这样）**不是**顶层块，照常逐个登记、逐个清除。`set`、`keep` 的语义不变，整字段 `headers` 的 SecretEdit 仍非法。③**保存路径兜底（31b-10）**：`settings.apply` 修改已保存的渠道时，只要 `bridge_alerts.ENDPOINT_FIELDS`（`host`、`port`、`server`、`url`、`webhook_url`、`api_url`、`ssl`、`starttls`）中任一项与已保存值不同（两侧都按发送时的形状比较：行级扁平字段并入 `config`、`config` 优先，与 `alert_config.normalized_notification_channels` 相同；缺省的端点字段取 `alert_config.default_notification_channel_config(类型)` 的默认值，所以提交与默认值相同的值不算改动；文本去首尾空白，十进制 `port` 按整数比较），而某个已保存的非空凭据（`bridge_alerts.inherited_secrets`：该渠道的秘密字段与凭据请求头）在本次请求里没有 `set` 或 `clear` 的 SecretEdit（`keep` 或不提交都算沿用；`set` 同一个值算重新填写），整节以 `validation`、`message_key=errors.settings.alerts_endpoint_changed`、`reason_code=alerts_endpoint_changed`、`hold:true` 拒绝，`field`/`params.field` 指向第一个沿用的凭据：`alerts.channels[<序号>].config.<字段>`，凭据请求头为 `…config.headers.<名>`；名字是否可以上线与 ① 用同一条规则（同一个判定函数；显示过滤与 ① 同源，由 `bridge_runtime_access.display_filter` 登记验证器可见的全部秘密——本次保存前的各节后端快照，含**全部**提醒渠道而不只是被拒的这一条，加上本次提交的提醒渠道值；另外失败关闭地加入所有渠道行（已保存与提交）的标量秘密字段和每个请求头的值。已知边界：订阅源与运行时已加载的秘密不在验证器的输入里，不参与这一步过滤，只靠形状规则与帧敏感扫描兜底），不通过时退为 `…config.headers`；整条路径仍不合格时退为 `…config`；不落盘、rev 不前进。端点不变、新建渠道、已保存凭据为空时行为不变。这条规则与 `alerts.test{target_id}` 的 `alerts_test_endpoint_changed`（§17 T-P2-31b）共用同一对纯函数 `bridge_alerts.endpoint_changed` / `inherited_secrets`。

**SMTP 服务商预设（T-P2-H11，与 Qt 对等）**：`alerts.catalog` 中 `email_smtp` 的 `host`、`port`、`starttls`、`ssl` 四个字段带 `conditions.presets`（预设键 → 该字段的值，与声音预设同形），取自 Qt 同一来源 `notification_onboarding.SMTP_PROVIDER_PRESETS`：`gmail`、`google` 为 `smtp.gmail.com`、587、starttls 开、ssl 关；`outlook`、`office365` 为 `smtp.office365.com`、587、starttls 开、ssl 关；`custom` 各字段为 null（不预填，保留用户输入）。另带 `conditions.preset_aliases`=`{"google":"gmail","office365":"outlook"}`：取值与前面某个预设完全相同的键标为它的别名，页面可以只显示 Qt 的三项（自定义、Gmail、Outlook / Microsoft 365）。预设只是页面填入表单的便利值，配置里没有 `provider` 字段，也不写入任何预设名；显示名用页面侧消息 ID `catalog.alerts.email_smtp.preset.<键>`（与 `catalog.alerts.sound.preset.<键>` 同一命名，后端不下发 label_id）。

成功终态补充：geo.update 必须 triggered/completed=true、status.running=false、errors=[]；install 必须 verified/handoff_pending=true；diagnose 必须 cleanup_pending=false 且 reason_code=null（status_applied 仍独立，过期目标可保留 false）；settings.apply 不接受空 sections，某节失败必须 failed 并保留其他已保存事实。exit.verify 仅 ok/partial 且绑定未失效才 done，其余为 failed/cancelled/superseded；partial 的双栈子结果与失败原因必须保留。

结果一致性：IdentityResult 的 ok 要求两栈 ok，partial 要求一栈 ok/一栈 failed；fresh=true 必须有 binding_id 和 aggregate observed_at_ms，成功子栈必须有地址与观测时间。update_available=true 必须有版本，installable=true 必须确有更新。gateway applied=true 必须有实际 selection/effective，不能同时 missing；Core 未运行时 selection 为已保存的选择意图。settings.validate 的 valid 与 errors 为空等价；onboarding complete 的成功响应为 configured，cancel 为 cancelled/missing。新 saveGrant/import.preview/update.prepareExit 的 expires_at_ms 必须晚于 produced_at_ms。非 stale 的 failed 启动态不得拼接 ready/serving；Core 缺失/未运行原因不得配 Core.running=true。停止抢占以 stopped/stop_requested 事实和 superseded 原因呈现，不显示普通启动失败。

| DTO | 字段（闭集） |
|---|---|
| Error | `{"code":"code","message_key":"message","params":"object","field?":"str","hold?":"bool","detail?":"str","reason_code?":"str"}` |
| CollectTier | `{"tier":"tier","generation":"uint","first_check":"unknown&#124;running&#124;complete","source_count":"uint?","healthy":"uint?","required":"uint?","observed_at_ms":"uint?"}` |
| Listener | `{"kind":"mixed&#124;http&#124;socks&#124;controller&#124;subscription","host":"str","port":"port","available":"bool?"}` |
| Diagnostic | `{"request_id":"str","mode":"direct&#124;chain","state":"running&#124;ok&#124;failed&#124;cancelled&#124;superseded","latency_ms":"number?","reason_code":"str?","status_applied":"bool","cleanup_pending":"bool"}` |
| DropCounts | `{"debug":"uint","info":"uint","warn":"uint","error":"uint","critical":"uint","total":"uint"}` |
| ConfigValues | `{"network?":"object","dns?":"object","routing?":"object","latency?":"object","sources?":"[Source]","alerts?":"object","ui?":"object","autostart?":"object","traffic?":"object","killswitch?":"object","bastion?":"object"}` |
| SecretEdit | `{"field":"str","target_id?":"str","action":"set&#124;clear&#124;keep","value?":"text"}` |
| ConfigChange | `{"section":"network&#124;dns&#124;routing&#124;latency&#124;sources&#124;alerts&#124;ui&#124;autostart&#124;traffic&#124;killswitch&#124;bastion","values":"object","secrets?":"[SecretEdit]"}` |
| SectionResult | `{"section":"str","saved":"bool","applied":"bool","effect":"none&#124;ui&#124;autostart&#124;recheck&#124;reload&#124;restart&#124;deferred","error":"Error?"}` |
| PendingApply | `{"intent_id":"str","config_rev":"uint","generation":"uint","state":"pending&#124;applying&#124;applied&#124;failed&#124;superseded&#124;cancelled","effect":"recheck&#124;reload&#124;restart","error":"Error?"}` |
| SaveResult | `{"rev":"uint","sections":"[SectionResult]","pending":"PendingApply?"}` |
| Applied | `{"saved":"bool","applied":"bool","apply_error":"Error?","pending":"PendingApply?"}` |
| Node | `{"id":"str","name":"str","tier":"tier","kind":"manual&#124;subscription&#124;automatic","enabled":"bool","status":"unknown&#124;pending&#124;healthy&#124;failed&#124;disabled","reason_code":"str?","protocol":"str","latency_ms":"uint?","subscription_id?":"str?"}`；`subscription_id`（K-4，T-P4-C1 第 3 项）= 订阅节点所属订阅的 `Subscription.id`（P2-06 owner 快照的成员关系，与订阅行 `total` 及其检测回执的 `member_ids` 同源；几个订阅共有同一节点时取已启用的、配置顺序在前的那个），其余种类为 null；缺席（冻结侧车）时页面不做订阅归组。T-P4-C2：`slow?:bool`（K-5）= 运行时延迟策略自己判的「较慢」（`tester.py` 的 `NodeStatus.SLOW`，严格层为超过上限一半），只在 `status=healthy` 时可能为 true（帧校验 `invariant:<路径>.slow`），「当前」标记仍由页面按 `route.published` 推；`last_checked_at_ms?:uint?`（K-2）= 运行时最近一次检测该节点的时间（`node.last_checked_at`），没有检测过为 null。缺席时页面不显示「较慢」和「N 秒前检测」 |
| Subscription | `{"id":"str","name":"str","tier":"tier","enabled":"bool","display":"str","total":"uint","healthy":"uint","visibility":"unknown&#124;pending&#124;current&#124;previous","refreshing":"bool","recheck?":"Recheck","last_refresh_at_ms?":"uint?"}`；T-P4-C2 第 6 项（K-6）：`recheck` 只在已启用订阅有成员处于 `pending`（待复检）时出现，`done` = 不再待复检的成员数，`total` = 成员数（与 `total` 同一成员关系；与该订阅成员节点行的 `pending` 状态同一证据），没有复检时省略；`last_refresh_at_ms` = 该层订阅缓存最近一次刷新的时间（Manager 的 `tier2_last_refresh_at`/`exit_last_refresh_at`，**层级粒度**：同层所有订阅刷新进同一份缓存，不是逐个订阅），只有 Sentinel 和 Gateway 两层有这个时间，其他层（Vanguard、Bastion）的订阅行省略；从未刷新为 null，运行时读不到时省略 |
| Recheck | `{"done":"uint","total":"uint"}`；`done ≤ total`（T-P4-C2） |
| Endpoint | `{"host_masked":"str","port":"port"}`；只在 `nodes.detail` 出现（T-P4-C2 第 3 项，K-1）：`host_masked` 是侧车生成的掩码，域名保留首字符和顶级标签（`gw-tyo01.example.org` → `g•••.org`），IPv4 保留第一段（`203.•••`），IPv6 保留第一组（`2001:•••`），首字符不是 ASCII 字母或数字时用 `•`；帧校验以正则 `^[A-Za-z0-9•]•••(\.[A-Za-z][A-Za-z0-9-]{0,62})?$&#124;^[0-9]{1,3}\.•••$&#124;^[0-9a-fA-F]{1,4}:•••$` 限定形状（`invariant:<路径>.host_masked`）。完整地址不在 v1 线上（`nodes.reveal` 白名单没有 `host`，见报告） |
| NodeProtocol | `{"name":"str","port":"port","node_id?":"str"}`；只在 `nodes.detail.protocols` 出现（T-P4-C2 第 5 项，K-3）：Vanguard 多协议物理节点的每个协议变体按回退顺序（Mihomo TIER1 组同一顺序）列出，`name` 是协议键（如 `vless_reality`、`hysteria2`，小写，页面自行显示名称），`port` 是该变体端口，`node_id` 是该变体在 `nodes.list` 里的 `Node.id`（可直接用于 `nodes.diagnose`，见报告 K-9）；其他节点只列它自己一项。不含任何凭据或地址 |
| ConfigItem | `{"id":"str","name":"str","kind":"str","section":"str","enabled":"bool","secret_set":"bool"}` |
| Source | `{"id":"str","kind":"str","display":"str","enabled":"bool","hint?":"str"}`；`hint` 只由侧车生成（U4，T-P4-C1 第 9 项）：github_repos 规范化为 owner/repo 后为 `<o>•••/<r>•••`，telegram_channels 为 `@<c>•••`，subscription_urls 为主机名首字符 `<h>•••`（不含 scheme、userinfo、端口、路径、查询串），local_files 为文件名首字符 `<f>•••`（不含目录）；首字符不是 ASCII 字母或数字时用 `•`；值为空或解析失败时省略。`config.snapshot.values.sources[]` 与 `bastion.sources.items[]` 同时带；不进 `secret_fields_set`，来源值本身仍是秘密；页面提交时校验（非空字符串）后忽略，与 `display` 相同；帧校验另以正则 `^@?[A-Za-z0-9•]•••(/[A-Za-z0-9•]•••)?$` 限定形状（`invariant:<路径>.hint`），生成逻辑出错时不会把来源原文发上线（修复第 1 轮） |
| IdentityLeg | `{"status":"unknown&#124;running&#124;ok&#124;failed&#124;cancelled","address":"str?","country":"str?","reason_code":"str?","observed_at_ms":"uint?","city?":"str","timezone?":"str","asn?":"str","org?":"str","type?":"backbone&#124;business&#124;hosting&#124;mobile&#124;residential&#124;isp","single_source?":"bool"}`；T-P4-C2 第 1 项（G07，overview R1）：这些上下文只出现在 `status=ok` 的一栈（`invariant:<路径>.context`），只取 `overview_ip_check` 为这个观测地址附带的来源报告（ident.me）：`city`/`timezone` 只在位置报告一致（`location.state=reported`，与 `country` 同一规则）时给，`timezone` 是 IANA 时区名或 `UTC`；`asn` 是不带 `AS` 前缀的十进制数字串；`org`（运营商/组织）与 `city` 是**不可信外部文本**，只能按文本显示，帧敏感扫描会拒绝的值不发送；`type` 是来源报告的网络类型；`asn`/`org`/`type` 只含各来源一致的字段（有冲突的字段本来就不在）；`single_source` = 只有一个稳定来源确认了这个地址（该地址族状态 `unconfirmed`，Qt 的「单一来源」徽标），`false` = 至少两个来源一致。字段缺席 = 未报告，页面隐藏对应行 |
| IdentityResult | `{"request_id":"str","generation":"uint","binding_id":"str?","status":"idle&#124;running&#124;ok&#124;partial&#124;failed&#124;cancelled&#124;superseded","reason_code":"str?","v4":"IdentityLeg","v6":"IdentityLeg","fresh":"bool","observed_at_ms":"uint?","native_fallback_verified":"bool","native_fallback_verified_commit_rejected":"bool","native_fallback_unverified_budget":"bool","manual_execution_binding_stale":"bool","trigger?":"auto&#124;manual&#124;recheck"}`；`trigger`（T-P4-C2 第 2 项，overview R3）= 发起这次查询的是谁：`manual`（`exit.verify`，「已验证」）或 `auto`（自动查询，「已确认」）；由出口 owner 在写入 `exit.identity.current` 时加上，随结果进入 `previous`，页面重开后仍可区分；`idle` 结果和 `exit.verify` 作业结果不带；`recheck` 为合同保留值，v1 侧车不产生。缺席时页面按「本窗口自己的 request_id 记为已验证」降级 |
| RefreshResult | `{"total":"uint","healthy":"uint","stored":"bool","applied":"bool","retained_previous":"bool","apply_error":"Error?","error":"Error?"}` |
| Alert | `{"id":"str","seq":"uint","severity":"info&#124;warn&#124;error&#124;critical","event_id":"str","title":"text","detail":"text","ts_ms":"uint"}` |
| Notice | `{"kind":"outage&#124;capture_lost&#124;start_failed&#124;fallback&#124;unstable&#124;update&#124;preview","incident":"str","state":"open&#124;update&#124;resolved&#124;closed","group":"connection&#124;tier_fallback&#124;update&#124;preview","title_id":"str","detail_id?":"str","reason_id?":"str","fix_id?":"str","params":"object","action?":"view","sound":"bool","sticky":"bool","ttl_ms":"uint"}`；提醒重设计（10-30）：ID 为界面消息 ID（`notice.*`、`errors.start.*`），属元数据路径；同一事故的 update/resolved/closed 沿用同一 `incident` |
| Log | `{"seq":"uint","ts_ms":"uint","rx_ms":"uint","key":"str","source":"TESSERACT&#124;MIHOMO&#124;ALERT&#124;UNKNOWN","level":"debug&#124;info&#124;warn&#124;error&#124;critical","category":"str","title":"text","detail":"text","raw":"text","is_key_event?":"bool","run_id?":"str"}`；`key` 无事件键时为 `"log.raw"`；`is_key_event`、`run_id` 的语义与缺席时的退路见 §5.5 |
| LogFilter | `{"levels?":"[str]","sources?":"[str]","categories?":"[str]","text?":"text"}` |
| LogPage | `{"records":"[Log]","watermark":"uint","next_cursor":"str?","gap":"bool","dropped":"uint"}` 使用显式边界代替切换 gap：key=source_boundary 的 Log 在 detail/raw 中保存 JSON（from/to 为 stdout、mihomo_api 或 null，at_ms、reason、continuity="unknown"）；范围内边界不受过滤条件隐藏，gap/dropped 仍只表示淘汰损失，切换窗口不承诺零重复或零遗漏。 |
| Geo | `{"running":"bool","updated_at_ms":"uint?","errors":"[Error]","items?":"[GeoItem]"}`；`items`（T-P4-C2 第 11 项，settings R-11）按更新器的固定顺序列出它有事实的数据种类（下载成功过或尝试失败过）；缺席时页面合并为一行（R-11 降级） |
| GeoItem | `{"kind":"geoip&#124;geosite&#124;mmdb&#124;asn","updated_at_ms?":"uint?"}`；`updated_at_ms` = 该种类最近一次下载成功的时间，只失败过为 null；错误原文（可能含下载地址、路径）从不投影 |
| Update | `{"state":"idle&#124;checking&#124;available&#124;downloading&#124;verified&#124;handoff&#124;failed","version":"str?","update_available":"bool","installable":"bool","error":"Error?","current_version?":"str?","previous_version?":"str?"}`；`current_version` = 正在运行的版本；`previous_version` 只在本次启动是刚完成的更新时有值（经验证的更新回执记录的旧版本，侧车启动时读一次并标记已提示，本 epoch 内保持；G51「更新完成」提示据此显示），否则为 null；`previous_version` 非 null 时 `current_version` 必须有值（T-P4-C1 第 7 项）。**已知限制**：「已提示」在侧车注册时（首个 `update.state` 发出之前）就写入 Qt 共用的一次性记录；侧车在页面显示前崩溃或被外壳重启时，提示会丢失一次，等页面/外壳确认显示后再标记属于后续单（修复第 1 轮 S3；已裁定：v1 已知限制，决定_第五波裁定 C1-#8）。T-P4-C2 第 10 项（settings R-09）：`notes?:text?` = 发布说明的应用内部分（`app_updater.extract_app_release_notes`），**纯文本、不可信**，页面只能按文本渲染（不解析 Markdown/HTML、不生成链接）：侧车去掉控制字符和 `http`/`https` 链接的协议头（`https://x/y` → `x/y`；其他协议头，例如节点 URI，或者带用户信息的链接，整段说明不发送，按原文判断）、最多 8000 个字符（超出以 `…` 结尾），帧敏感扫描仍会拒绝的说明不发送（null）；只在有可用更新时有值。`enabled?:bool` = 本构建是否启用应用内更新（`app_info.APP_UPDATE_ENABLED`）。`last_check_at_ms?:uint?` = 本侧车 epoch 内最近一次检查（成功或失败）结束的时间，侧车启动后尚未检查为 null（不持久） |
| Recovery | `{"proxy":"not_needed&#124;restored&#124;not_owned&#124;permission_required&#124;failed&#124;unknown","firewall":"not_needed&#124;restored&#124;not_owned&#124;permission_required&#124;failed&#124;unknown"}` |
| FieldSpec | `{"id":"str","label_id":"str","type":"string&#124;integer&#124;number&#124;boolean&#124;choice&#124;multichoice&#124;secret&#124;textarea","required":"bool","choices":"[str]","conditions":"object"}` |
| ProtocolSpec | `{"id":"str","label_id":"str","fields":"[FieldSpec]"}` |
| ImportGroup | `{"key":"str","label?":"str","row_indexes":"[uint]"}`；`import.preview.groups` 的一项（T-P4-C2 第 13 项，G38，语义见 §15）；`row_indexes` 非空、组内不重复，跨组不重复且都小于 `accepted`（`invariant:data.groups`）；`label` 最多 128 个字符；`key` 只能是 `g` 加 1–6 位数字（`invariant:<路径>.key`，它不做帧敏感扫描，所以形状固定） |

fixtures/valid 保留 P1；fixtures/v1/valid 各命令有请求与结果、各 job 有 ack、各查询有请求与响应、各新增主题有 evt；fixtures/v1/invalid 是带预期错误的反例。仅用虚构秘密与 ID，不含现场配置。

## 17. 作废项、改名项与兼容边界

T-P2-27（Claude 10-05 裁定）：仅壳主题 `update.prepareExit` 的 data 增加 `installer_path`、`sha256`、`args`（形状与不变量见 §11 该行），供外壳在侧车真实退出后自行复核字节并启动安装器；§13 中 `prepareExit{…}` 的字段列举以 §11 为准。页面永远看不到该主题；现有敏感值扫描不拒普通 Windows 绝对路径，因此未增加私密例外位置。修复第 1 轮（Claude 10-05）：回到 §13 原设计——`update.exitResponse` accepted=true 只记录接管并立即回 `{recorded:true}`，侧车不提交、不请求也不等待退出，由外壳收到该回复后自己发 `app.quit{reason:update}`；侧车只读观察退出是否开始，接管后 30 秒内未开始退出则撤销接管回到 verified（error.code=lifecycle_busy），收据保留。同时收紧该主题不变量：installer_path 仅盘符形式绝对路径，args 每项须为 `/[A-Z][A-Z0-9_]{0,31}` 开关。

T-P2-31b（集成二）：`hello.caps` 改为 v1 已接线全集（§3）；`alerts.desktopDelivered` 的执行域在规格表中改为 control 并删除执行器覆盖表，业务码增加 `timeout`（写盘超时答复）；`shell.saveGrant.args` 增加可选 `for_owner`；`alerts.test.args` 增加可选 `target_id`、`secrets`（SecretEdit，含 keep）；`alerts.desktop` 增加可选 `force`（外壳兼容一版 `desktop-test-` 前缀）；校验失败但 `id` 可信的请求回结构化 error（§10）；§13 新增「外壳—页面合同」，§13 的 prepareExit 字段与 §11 同步；§14 端口冲突改为方案 a；§15 补来源批量四点保证、启动中视为运行中、受控写入推进持久 rev。修复第 1 轮：§13 承载增加即时拒绝 `frame_too_large`（请求帧加信封余量超过单帧上限）与写端兜底；saveGrant 的信封 owner 为 `shell`，for_owner 已撤销时答 `not_allowed`；§10 区分信封枚举（`invalid_enum`）与参数形状（`invalid_type`）；§12 alerts.test{target_id} 改动端点时不得沿用已保存秘密（`alerts_test_endpoint_changed`）。

**v1 增补（T-P4-C1/C2），不升 proto**（批评 B10）：以下字段全部可省略（冻结侧车、旧帧与既有 fixture 照常合法），缺席时前端按各条注明的方式降级；`hello.caps` 不变（都是已有主题与命令的字段扩展），既有 fixture 不改，新例子一律是 `fixtures/v1/{valid,invalid}/p4c1_*.json`。T-P4-C1 逐项：①`ConfigChange.section` 增加 `bastion`，`ConfigValues` 增加 `bastion?{enabled,min_alive_nodes}`（§16）；②`Log`/`logs.batch.records[]` 增加 `is_key_event?:bool`、`run_id?:str`，原始行 `key` 统一为 `"log.raw"`，实时批次关键事件标题按当前语言渲染（§5.5）；③`Node` 增加 `subscription_id?:str?`（§16）；④`config.snapshot` 投影补齐：`killswitch` 的 `allow_lan`/`allow_dns`/`dns_whitelist`/`additional_whitelist`、`network.egress_recovery{enabled,retry_source_refresh}`、DNS 解析器地址豁免、提醒渠道 `config` 的全部非秘密字段、`ui.start_in_tray_on_autostart` 的写入校验（§16）；⑤`killswitch.state` 增加 `triggered?:bool`（§11）；⑥首次运行语言（D4）只在侧车首次创建配置时写 config.local.yaml 的 `ui.language`，无线上字段；⑦`Update` 增加 `current_version?:str?`、`previous_version?:str?`（§16）；⑧`nodes.update` 对已停用节点按 ConfigItem 稳定 ID 解析（已有行为，§16 写明）；⑨`Source` 增加 `hint?:str`（§16）。修复第 1 轮：渠道 `alert_types` 投影生效过滤（有费用渠道未设置时显示仅紧急默认）、列表字段整字段扣下（字段级 `secret_fields_set` ID）、原样回存范围收窄到可编辑字段、日志续页游标绑定语言、`Source.hint` 形状校验（§5.5、§16）。另：`tiers.summary.order` 写明含 `gateway`（§5.3）；`hello.caps.features` 的名字是 B 项编号（`B-31`、子项 `B-30a`，与前端登记表和外壳 `hgate.rs` 同一格式 `B-` 加 1–3 位数字加至多一个小写字母），`bridge_protocol.validate_hello` 拒绝其他写法与重复项（决定_第四波裁定 02a-4）。

**v1 增补（T-P4-C2），不升 proto**：规则同上一段（全部可省略、`hello.caps` 不变、既有 fixture 不改），新例子是 `fixtures/v1/{valid,invalid}/p4c2_*.json`。逐项：①`IdentityLeg` 增加 `city?`、`timezone?`、`asn?`、`org?`（不可信文本）、`type?`、`single_source?`，只在 ok 栈（§16）；②`IdentityResult` 增加 `trigger?:auto|manual|recheck`（§16）；③`nodes.detail` 增加 `endpoint?:Endpoint?`（掩码地址与端口，§11/§16），`Node` 增加 `last_checked_at_ms?`，`tiers.summary.tiers[层]` 增加 `check_interval_s?`（§5.3）；④`Node` 增加 `slow?:bool`（§16）；⑤`nodes.detail` 增加 `protocols?:[NodeProtocol]`（§16）；⑥`Subscription` 增加 `recheck?:{done,total}`、`last_refresh_at_ms?`（§16）；⑦`tiers.summary.tiers[层]` 增加 `pending?`（§5.3）；⑧`nodes.diagnose.args.protocol` **未增加**（运行时 `diagnose_node` 没有这个参数；改用 `NodeProtocol.node_id` 诊断某个协议变体，见 T-P4-C2 报告）；⑨`gateway.select` 的 `proxy_name`/`selected` 由 `str` 放宽为 `text`，`""` = 停用 Gateway，done 不变量相应放宽（停用时 `selection:null`），并写明 `proxy_name` 的取值与歧义（§10/§15）；⑩`Update` 增加 `notes?`（纯文本）、`enabled?`、`last_check_at_ms?`（§16）；⑪`Geo` 增加 `items?:[GeoItem]`（§16）；⑫`logs.feed` 增加 `reason?:controller_disabled|core_not_running|stream_error|null`（§11）；⑬`import.preview` 的结果增加 `groups?:[ImportGroup]`（§10/§15/§16）。另：`tiers.summary.tiers[层]` 的键改为闭集校验（`enabled/total/usable` 加上这两个可选键）。接线未完成的项（侧车代码已就绪，待集成单接线）：③ 的 `endpoint`、⑤（`bridge_wiring._nodes_detail` 合并 `bridge_nodes.node_detail_extras`）、⑪（`bridge_queries.geo_status` 加 `items`）、⑫ 的运行时事实（`bridge_logs.register(session)`），详见 T-P4-C2 报告「接线」一节。

T-P2-H2（后端小修，决定_第五波裁定）：§3 补退出顺序与「关闭业务 owner 有放弃上限」的定性说明（不写数值）；§10 owner 改为页面按连接生成、外壳原样转发，v1 外壳不发 `owner_revoke`；`logs.exportFiltered` 页面导出当前视图一律用 seqs；§11 停止态：`gateway.current` 发布已保存的选择意图（不标 stale），`bastion.pool` 没有基线；状态主题的快照（订阅屏障与 `snapshot_res`）发出后，它已覆盖的同主题旧边沿与 gap 直接丢弃，不再在快照之后补发，该主题此后的边沿 `seq` 在线上顺延、保持连续；被屏障扣住的 `snapshot_res` 出队时按当时的状态作答（有基线即答快照）；custom 作用域下 `mode.set` 设为非 system 以 `not_allowed`、`reason_code=scope_isolated` 拒绝且不落盘，每次加载与重载配置后重新施加该作用域的强制值；`geo.update` 在 Geo 更新被配置禁用时以 `conflict`、`reason_code=disabled`（`errors.geo.disabled`）失败，`triggered:false`；§11 `config.changed.files` 不是完整改动清单；§16 DNS 解析器公开判定改为失败关闭、字段级 ID 列表提交即整体替换、「更新完成」标为已裁定的 v1 已知限制。不升 proto，DTO 形状不变。

**v1 增补（T-P2-H5，决定_第五波裁定 T-P4-C2 第 1 条，K-10），不升 proto**：`gateway.select.args` 增加可选 `node_id`，`proxy_name` 由必填改为可选、保留作兼容，两者恰给一个（同时给出 `invalid_type`，都不给仍是 `missing_field`，与旧请求只缺 `proxy_name` 时的答复相同）；`node_id` 优先，解决按名字选择的三种歧义：跨层重名被加 `-2` 后缀、名字被脱敏改写、空名兜底不同。新增 reason_code `unknown_node`、`not_gateway_node`、`node_unusable`（均为 conflict，`errors.gateway.*`）。结果 DTO 形状不变；按 `node_id` 选择时 `selected`/`selection` 是脱敏后的显示名。`hello.caps` 不变，既有 fixture 不改，新例子是 `fixtures/v1/{valid,invalid}/p2h5_*.json`。旧侧车不认 `node_id`（按旧 schema 答 `missing_field`），新前端与冻结侧车随同一版本发布。

T-P2-H6（后端小修，决定_第六波裁定 T-P2-H2 第 4、5 条）：§11 `gateway.current` 在 Core 运行时开始发布（此前运行时缺事实版本，运行态从不发布）：配置、来源、节点秘密三个事实版本由运行时观察得到，不是写入方提交的回执；停止态只在 `id(config)` 与配置修订号都未变时发布已保存的意图，任一变化先撤下基线（gap）再重读。修复第 1 轮：重读的触发粒度是「凭据指纹」而非「列表对象」——某层列表对象或长度变了才在锁外重算该层指纹（uri/password/uuid/source/sources 与 `extra` 中脱敏规则会收集的值，约 1.4 ms/300 节点），指纹变了才推进；`bridge_gateway_status`（4 层各 300 节点约 19 ms CPU）因此只在首次订阅、真实事实变化与 30 秒复核时调用，Tier3 冷启动期间不再随每次列表替换以 1 Hz 运行。发布层：快照覆盖旧边沿时只读该主题的待发项；rescue 把 gap 整段补回时，归还 gap 预留的 seq，该主题之后的边沿 `seq` 仍连续（§4，外壳的 EventCursor 不会看到跳号）。不升 proto，DTO 形状、`hello.caps`、fixture 均不变。

**v1 增补（提醒重设计，10-30，`.build/提醒模块重设计_20261030.md` §10），不升 proto**：①提醒只在本机：删除命令 `alerts.onboarding.start/poll/complete/cancel`、查询 `alerts.catalog`、`alerts.deliveries`、主题 `alerts.delivery`、`alerts.onboarding` 及 DTO `Onboarding`、`Delivery`；②`alerts.test` 改为设置页「预览提醒」，参数 `{}`，结果不变，总以 `force:true` 发出；③`alerts.desktop` 增加可省略的 `notice?`（`Notice`）：卡片与应用内提醒按其消息 ID 与参数渲染，`title`/`detail` 仅作原生气泡的兜底文字；由侧车 `alert_policy` 按总览所示事实（service.state、tiers.summary、killswitch.state、network.mode.drift）判定，壳按前台与否在应用内提醒与卡片窗口之间路由；④`config.snapshot.values.alerts` 与 `ConfigChange{section:"alerts"}` 改为 `{notify:{enabled,events,sound}}`，`events` 取 `connection`、`tier_fallback`、`update`；保存时从 config.local.yaml 删除旧渠道键。
⑤壳侧：
- `host:notify` 原样带上 `notice`。
- 上文的「走 Windows 通知」现在改走提醒卡片窗口（label `notice`）；全屏、演示或勿扰时先暂存；卡片窗口不可用时仍走 Windows 通知。
- 同一事故改走卡片时，外壳会发自己的 `host:notify{delivery_id:"shell-clear-N", notice.state:"closed"}`，撤掉页面上的横幅。这种帧页面不确认，也没有回执。
- `host_notify_rendered` 增加可省略的 `shown`：`shown:false` 表示页面收下了但没有显示（例如总览页已经显示这一状态）。它照常回执，不播提示音。
- 卡片窗口只用自己的命令（`notice_ready`、`notice_rendered`、`notice_open`、`notice_dismiss`、`notice_resize`）和事件（`notice:show/close/clear/language`）与外壳通信，不属于页面协议；`HOST_INVOKES` 不变。细节见 `shell/README.md`「10-30 提醒卡片」。

**v1 增补（T-P2-H8，决定_第七波裁定 T-P2-H5 第 2、4 条），不升 proto**：§11 `gateway.current` 增加可省略的 `selected_node_id?`（`str`，24 位小写十六进制的 `Node.id`），由运行时只读 getter `bridge_gateway_selected_node_id` 用最近一次严格 reload 的 render receipt 把已保存的选择名反解回 Gateway 节点身份，反解不到、有歧义、receipt 已过期或 Core 停止时省略；新增不变量 `invariant:gateway.current.selected_node_id`（出现时 `selected` 必为字符串、`missing` 为 false、形状为 opaque id）。§15 的 `gateway.select` 选择语义一句改为指向 §10（node_id 已由 T-P2-H5 落实）。冻结侧车与既有 fixture 不带该字段，照常合法，页面缺这个字段时只以文本显示当前选择；`hello.caps` 不变，新例子是 `fixtures/v1/{valid,invalid}/p2h8_*.json`。

**v1 增补（T-GW-ID：Gateway 选择按节点身份保存，11-03），不升 proto**：DTO 形状、`hello.caps`、reason_code 与 fixture 都不变。语义变化：①`gateway.select` 的 node_id 保存节点身份（`tiers.exit.selected_node` = `{identity: "v1:<64 位十六进制>", label}`，标签在 `selected_proxy`，本版不再写 `selected`；旧版本写入的 `selected` 优先，按名字处理），渲染与读者都按身份挑成员，T-P2-H5 里「reload 后名字移位就 selection_unconfirmed」改为直接选中所选节点此刻的成员；②`gateway.current.selected` 在路由有效时是所选节点此刻成员的显示名，与 `effective` 一致；③身份对不上任何导出节点（凭据轮换、节点删除）时 fail closed（`[REJECT]`、`missing:true`、`selected` 为标签），不按名字退回；④旧的只存名字的选择按更严的名字规则：同族（`X` 与 `X-<n>`）有不止一个出口身份时 `[REJECT]`（此前会按位置选中其中一个，可能是另一个出口），由别的层级挤成 `-2` 的单个 Gateway 名字现在能找到它；⑤冻结侧车读 `selected or selected_proxy`，按自己的命名解析这个标签：降级后标签可能指向另一个同名 Gateway，或解析不到而 `[REJECT]`，降级后应重新选择一次；在旧版本里改选或清除会被新版本识别。

**v1 增补（T-P2-H11，决定_第九波裁定），不升 proto**：①`config.snapshot.secret_fields_set` 增加凭据请求头的逐个字段 ID `alerts.<id>.headers.<名>`（整字段 `alerts.<id>.headers` 保留）；②`SecretEdit` 的 `clear` 用在 `headers.<名>` 上改为删除这个键，名字未保存时以字段级 `validation` 拒绝（此前写入空值）；③`settings.apply` 新增提醒渠道的字段级拒绝 `reason_code=alerts_endpoint_changed`（`errors.settings.alerts_endpoint_changed`，`hold:true`）；④`alerts.catalog` 中 `email_smtp` 的 `host/port/starttls/ssl` 的 `conditions` 增加 `presets`、`preset_aliases`（以上均见 §16）。DTO 形状不变（`secret_fields_set` 本来是 `[str]`，`FieldSpec.conditions` 本来是 object），`hello.caps` 不变，既有 fixture 不改，新例子是 `fixtures/v1/{valid,invalid}/p2h11_*.json`。冻结侧车既不登记逐个请求头 ID、也不下发预设，对 `headers.<名>` 的 clear 仍写空值：页面只在看到 `alerts.<id>.headers.<名>` 时才提供逐个「清除」，缺席时按 T-P4-05e 的现状降级（端点改了先 hold、重填后警告），没有 `presets` 时不显示预设控件。

T-P2-03 新增外壳控制帧 `owner_revoke{id,owner}`，及无 op 的确认 ack；替代只有“窗口销毁撤销 owner”而没有线上通知的旧描述。作业 ack 仍必须带 op（配对校验），P1 命令/订阅流程不变。

| 旧方案表述 | v1 决议与依据 |
|---|---|
| §3.2 写 4 MB 单帧、每主题 `evt`，§3.12 写“一个主题一帧” | 4 MiB 只作解析硬上限，常规 256 KiB；统一 tick 一个多主题 `batch`。迁移方案 §0.11、ASK-01 §8 和 spike review §D 优先。 |
| §3.2 的部分 `req/ack/res` 样例未带 epoch，只写 `published_at_ms` | 全部帧带 epoch，生产/发送墙钟分离；不传可重新授予预算的 monotonic 截止时间。迁移方案 §0.11、ASK-01 §8 优先。 |
| §3.5 两条执行道，stop/quit 在队列外 | 明确为独立优先控制域、串行变更域、有界普通域；`request_stop()` 在控制 worker 执行，stop 仍可能等 lifecycle 锁。迁移方案 §0.10、ASK-01 §6 优先。 |
| §4.5 S1 readiness 有 `validating`，把 `serving/ready` 近似合并 | 以 `runtime_egress_recovery.py` 的七个枚举值为准；验证中用 `validation_active`。迁移方案 §0.7、ASK-01 §3 优先。 |
| §4.5 `route.current`、`logs.stream`，§4.4 的 `traffic.history` 为查询 | 工单 P1 名称分别为 `route.published`、`logs.batch`、订阅时一次的 `traffic.history` 主题；旧名不作为 v1 别名。RT-4 只投影已发布路由。 |
| §3.6 每 30 s/50 补丁发全量节点快照、补丁按最新值覆盖 | P1 不周期推全量节点，现有主题无 `items/id` 基线，故不发送 patch 帧；参考合并函数按接收端 `base_rev` 合并，失配用 `snapshot_req` 请求替代快照，供后续增量主题使用。迁移方案 §0.11、ASK-01 §8 优先。 |
| §3.8 将所有主题按正常节奏三倍判陈旧 | 仅周期发布的 `service.state` 和运行中的 `traffic.tick` 使用静默超时；变化触发主题保留上次观察值，不以空闲误判 stale。 |
| §4.5 `tier.transition` 被当成完整边沿；§3.12 说边沿立即推 | P1 仅观察层级变化；权威边沿立即记录、下一 tick 优先投递，生产者不唤醒桥接线程。迁移方案 §0.9、ASK-01 §5 优先。 |
| §4.7 允许页面发多数 C/Q 命令 | P1 阶段只定义四个作业命令和取消；现已由 §10 冻结 v1 全集，app.quit 仍仅外壳发。 |

`fixtures/valid/*.json` 是完整可解析帧。`fixtures/invalid/invalid_utf8.json` 和 `oversize.json` 是生成原始非法字节的描述文件（非法 UTF-8 不能作为合法 JSON 文件直接保存）；`base_rev_mismatch.json` 包含接收端基线和补丁，由参考合并函数判失配。`missing_base_rev.json` 演示保留补丁形状的缺字段错误，但 P1 的任何 patch 帧都会因 topic/kind 配对被拒；其余 invalid fixture 是直接供帧校验的反例。`valid/sensitive_*.json` 是自由文本扫描命中但允许发送的诊断样例，并非生产端脱敏的合格输出。

裁定已采纳的名称不再标“待确认”，没有旧名别名。终态容量与保留数仍为 64 未终态、最近 256 项且至少 10 分钟。运行时/冻结/真实网络门禁属于后续工单，本合同及 fixtures 不能代替这些验收。

追加作废项：①“约 5 秒安全路由刷新”作废，界面只读已发布快照，注册表 5 秒漂移观察与路由刷新无关；②RT-6 比较 history 长度作废，改事件序号/追加钩子；③不完整 onboarding 枚举作废，以 §16 八态为准；④`tier.transition` 的权威切换承诺作废，目前只准 `authority=observed`；⑤“hello 前恢复”改为“RT-1 在构造运行时之前，hello 在构造之后”；⑥`nodes.detail(reveal?)` 作废，改 `nodes.reveal`；⑦“统一保存只做最重的一项/跨文件全回滚/必然只 reload 一次”作废，逐节结果加 pending 和独立副作用；⑧`audit.startup` 页面/普通 IPC 命令作废，保留 guarded 内部动作；⑨`app.quit(reason=endsession)` 作废，注销改 `app.sessionEnd`；⑩onboarding complete 自动保存/启用渠道作废，仅返回草稿；⑪“发布新版本通知即已提醒”作废，成功展示回执后记最近 16 版本；⑫ASK-02 实验目录建议由裁定改为 `.build/claude-p2-ab/`，不碰 R8；⑬自动池诊断首发拒绝，不借诊断写 manager；⑭`autostart.state.command` 的页面原始路径投影改为 `target/mismatch`，真实命令由外壳受信安装布局核验。
