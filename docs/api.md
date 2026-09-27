# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

### 诊室、设备与清洁准备间隔

- `POST /resources/rooms`、`POST /resources/devices` 登记诊室与治疗设备；`turnover_minutes` 是服务结束后的清洁/恢复准备分钟数（诊室必须为正数，设备可为 0）。`GET /resources` 列出资源；`POST /resources/{id}/disable` 以 `expected_version` 停用并推进资源版本。
- 创建预约可附 `resource_ids`（诊室与设备编号数组）。占位即为每项资源生成占用窗口：从服务开始直到服务结束加该资源的清洁准备间隔。人员、房间与设备的重叠判断使用同一套绝对时间比较。
- 确认（`book`）前会复核全部资源：任一资源版本在占位期间变化（例如设备被停用）或出现新的占用冲突，整次确认失败，预约转为取消，全部临时占位在同一事务内释放，响应的 `details` 说明是哪个资源。
- `POST /appointments/{id}/reschedule` 以 `expected_version` 原子改期：旧占用释放与新窗口占用在同一事务完成；目标时段冲突时整个事务回滚，原预约保持不变。
- 释放规则因结局而异：取消立即释放全部资源；未到诊立即释放设备、房间保留至原定结束时刻（为迟到到达留缓冲）；完成服务立即释放设备、房间保留至清洁准备窗口结束。到点的房间无需后台任务即自动停止阻塞新预约。
- `GET /appointments/calendar?date=YYYY-MM-DD` 按诊所时区返回当地日期的预约日历，每项预约同时给出 UTC 与诊所本地起止时间；跨越当地午夜或夏令时切换的预约按诊所时区换算展示，重叠判断始终基于绝对时刻。
- `GET /appointments/conflicts?starts_at=&ends_at=&staff_id=&resource_ids=` 是运营查询：返回候选时段与现有占用的冲突，逐项说明冲突来自哪个人员/诊室/设备、被哪笔预约的哪个时间段（UTC 与诊所本地时间）占用。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
