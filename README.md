# 大地测量网平差

一个仅使用 Python 标准库实现的大地测量观测管理与平差服务。支持角度、边长、高程差、已知点和未知点近似坐标，使用加权最小二乘求解，并输出残差、单位权中误差和点位精度。

## 运行

需要 Python 3.11+。

```bash
python app.py --init
python app.py --port 8006
```

浏览器打开 <http://127.0.0.1:8006>。数据库默认写入 `geodetic.db`，可以通过 `--db` 或环境变量 `GEODETIC_DB` 修改。旧库启动时会自动补齐新表/新列，不需要手工迁移。

`--init` 会初始化一个包含 A、B 已知点和 C、D 待求点的示例期次。重复执行不会重复添加。

## 异常复核台

平差后残差超过 **3.5 倍单位权中误差**（|v|√w/σ₀）的观测逐条生成异常台账，每条都给出：

- **残差与限差**：角度以角秒、距离/高差以米展示，同时写明观测值、σ₀ 和超标倍数；
- **原因**：如「高差 K1-U：标准化残差 3.94σ 超过限差 3.50σ（…限差 ±0.4001m…）」。

作业员对每条异常二选一：

- **安排复测**：原观测立即停用（状态 `superseded`，行不删除），再回填复测值，系统用新观测重算；
- **填依据排除**：依据不少于 5 个字，观测置 `excluded` 留档，随后自动重算。

重算若**有效观测不足（少于 2 条）或法方程秩亏/不可逆**，整次重算通过 SAVEPOINT 回滚：
**沿用上次成果、结果表不清空**，接口返回拒因（422，形如「重算未通过，沿用上次成果。拒因：…」），异常回到「待处理」，
但已填写的依据和「尝试—拒绝」经过都会保留。复测值若导致无解，新观测以 `rejected_remeasure` 留档、不采用。

重算后残差回到限差内的未决异常自动置为「重算合格」。**异常未全部处理完或尚未平差时，不能提交复核。**
审核员退回后，异常台账、排除依据、处理经过和原观测全部保留，期次回到草稿可再次提交。

分层约定（三者互不嵌套，便于单独测试）：

- `anomaly.py`：判定层，纯函数（限差、异常原因、提交闸门、相邻批次前后变化），不 import 业务模块；
- `app.py`：存储、平差计算、事务与 HTTP 接口；
- `static/index.html`：页面，原生 HTML/CSS/JS。

## 主要 API

所有修改接口使用 `X-User`、`X-Role` 请求头区分身份，角色为 `editor`、`reviewer` 或 `viewer`。

- `POST /api/epochs`：创建期次。
- `POST /api/epochs/{id}/points`：设置已知点或未知点近似坐标。
- `POST /api/epochs/{id}/observations`：录入 `distance`、`angle`、`height_difference` 观测；疑似重复值会返回 409。
- `POST /api/epochs/{id}/adjust`：迭代加权最小二乘平差；超差观测标记为 `outlier` 但**仍参与平差**，等待复核台处理。
- `GET  /api/epochs/{id}/anomalies`：逐条异常（残差、限差、原因、依据、处理经过）。
- `GET  /api/epochs/{id}/anomalies/{aid}`：单条异常详情。
- `POST /api/epochs/{id}/anomalies/{aid}/schedule-remeasure`：安排复测并立即重算（`{"note":?}`）。
- `POST /api/epochs/{id}/anomalies/{aid}/exclude`：填依据排除并重算（`{"basis":"…"}`）。
- `POST /api/epochs/{id}/anomalies/{aid}/complete-remeasure`：回填复测值并重算（`{"value":…,"weight"?}`）。
- `GET  /api/epochs/{id}/review-bundle`：审核视图：异常依据与经过、相邻批次前后变化、本次采用集合、批次历史。
- `POST /api/epochs/{id}/transition/submit|approve|reject|publish`：审核发布状态机。
- `GET /api/epochs/{id}/results`：查看点位坐标和精度。
- `GET /api/epochs/{id}/compare/{other_id}`：比较两期成果。
- `GET /api/epochs/{id}/audit`：查看操作审计。

## 业务约束

- 只有在 `draft` 状态且角色为 `editor` 可修改点、观测、平差和处理异常；提交后必须由非创建人、非提交人的 `reviewer` 批准才能发布。
- 存在「待处理 / 已安排复测」异常，或没有任何成功平差批次时，提交复核返回 409。
- 同类型、同端点、容差内且处于采用状态的观测被视为重复；确实需要保留时可在请求体传 `allow_duplicate: true`。
- 法方程秩亏或坐标不可解时返回 422，不会写出平差结果；异常处理触发的重算被拒时沿用上次成果。
- 观测只有状态流转（valid/outlier/excluded/superseded/rejected_remeasure），从不物理删除；每次重算保存为一个 `adjust_runs` 批次并附带当时的采用集合快照。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整示例平差、提交审核发布、重复观测冲突、越权操作，以及异常台账与限差原因、
依据排除与重算、复测留档与新观测采用、重算被拒沿用上次成果、提交闸门、退回后记录保留和判定层纯函数。
