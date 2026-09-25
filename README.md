# 大地测量网平差

一个仅使用 Python 标准库实现的大地测量观测管理与平差服务。支持角度、边长、高程差、已知点和未知点近似坐标，使用加权最小二乘求解，并输出残差、单位权中误差和点位精度。

## 运行

需要 Python 3.11+。

```bash
python app.py --init
python app.py --port 8006
```

浏览器打开 <http://127.0.0.1:8006>。数据库默认写入 `geodetic.db`，可以通过 `--db` 或环境变量 `GEODETIC_DB` 修改。

`--init` 会初始化一个包含 A、B 已知点和 C、D 待求点的示例期次。重复执行不会重复添加。

## 主要 API

所有修改接口使用 `X-User`、`X-Role` 请求头区分身份，角色为 `editor`、`reviewer` 或 `viewer`。

- `POST /api/epochs`：创建期次。
- `POST /api/epochs/{id}/points`：设置已知点或未知点近似坐标。
- `POST /api/epochs/{id}/observations`：录入 `distance`、`angle`、`height_difference` 观测；疑似重复值会返回 409。
- `POST /api/epochs/{id}/adjust`：迭代加权最小二乘平差；残差超过 3.5 倍单位权中误差的观测标记为 `outlier`。
- `POST /api/epochs/{id}/transition/submit|approve|reject|publish`：审核发布状态机。
- `GET /api/epochs/{id}/results`：查看点位坐标和精度。
- `GET /api/epochs/{id}/compare/{other_id}`：比较两期成果。
- `GET /api/epochs/{id}/audit`：查看操作审计。

## 业务约束

- 只有在 `draft` 状态可修改点、观测和重新平差；提交后必须由非创建人、非提交人的 `reviewer` 批准才能发布。
- 同类型、同端点、容差内的观测被视为重复；确实需要保留时可在请求体传 `allow_duplicate: true`。
- 法方程秩亏或坐标不可解时返回 422，不会写出平差结果。
- 每期结果独立保存，发布只改变期次状态，不删除历史观测。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整示例平差、提交审核发布、重复观测冲突和越权操作。
