# 运行手册 供应商主数据台账

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
```

首次打开任意页面会按内置样例自动采信一次，随后逐级计算归一名、分块、候选对与合并簇。

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

`-v` 会删除命名卷 `merge-data`，批次表与复核决定一并清空。

## 重跑与幂等

- `POST /api/suppliers/import` 带 `force: true` 时按同一批文件重算。
- 批次号由输入摘要（按文件名排序的 sha256 拼接）与规则版本共同决定，同一批文件得到同一个 `run_id`；
  命中已有批次时直接复用结果，不新增记录。
- 复核决定写入后，簇与金额会立即重算，不需要手工触发。
- 编码复用案件号由集团编码决定（`SC-000202`），重跑或增删文件都不会让已批准案件错位。

## 编码复用接替

```bash
# 加载演示批次（六份主档 + succession_g_lifecycle.csv）
curl -s 'http://127.0.0.1:18205/succession?batch=lifecycle' -o /dev/null

# 看待判案件、接替依据、受影响记录与发布计划
curl -s http://127.0.0.1:18205/api/succession | python -m json.tool
curl -s http://127.0.0.1:18205/api/succession/plan | python -m json.tool

# 逐案批准（verdict: successor | separate，expected_version 取当前版本）
curl -s -X POST http://127.0.0.1:18205/api/succession/SC-000202/decision \
  -H 'Content-Type: application/json' \
  -d '{"verdict":"successor","expected_version":0}'

# 一次性切换：payload_sha256 与 expected_prior_version 必须来自当前计划
curl -s -X POST http://127.0.0.1:18205/api/succession/publish \
  -H 'Content-Type: application/json' \
  -d '{"payload_sha256":"<计划摘要>","expected_prior_version":0}'

# 查看正式主数据版本、回执与快照；需要回退时恢复为新版本
curl -s http://127.0.0.1:18205/api/succession/master | python -m json.tool
curl -s http://127.0.0.1:18205/api/succession/receipts | python -m json.tool
curl -s http://127.0.0.1:18205/api/succession/snapshots | python -m json.tool
curl -s -X POST http://127.0.0.1:18205/api/succession/restore/1
```

发布语义：

- 有任一案件待判/未批准时计划 `ok=false`，`publish` 返回 `S_NOT_APPROVED` 并列出阻断案件。
- 页面停留期间数据变化：`payload_sha256` 对不上报 `S_PAYLOAD_DRIFT`；
  正式主数据已升级：`expected_prior_version` 对不上报 `S_STALE_MASTER`。
- 发布请求重复提交：同一批、摘要与旧版本都对得上时返回原回执（`replayed: true`），
  不新增版本、不重复写台账；摘要或旧版本不符按 `S_PAYLOAD_DRIFT` / `S_RECEIPT_CONFLICT` 拒绝。
- 写库中途失败：`S_PUBLISH_FAILED`，事务整体回滚；旧版整版仍在快照表，可直接恢复。
- `restore` 不覆盖、不删除任何版本；恢复结果是一个新版本，原版本标 `superseded`。

## 常用请求

```bash
curl -s -X POST http://127.0.0.1:18205/api/suppliers/import -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:18205/api/reconcile
curl -s http://127.0.0.1:18205/api/export/master.csv -o master.csv
curl -s http://127.0.0.1:18205/api/export/manifest.json -o manifest.json
```

判定一张复核单：

```bash
curl -s -X POST http://127.0.0.1:18205/api/reviews/P-00105/decision \
  -H 'Content-Type: application/json' \
  -d '{"decision":"merge","expected_version":0}'
```

## 失败码处理

| 原因码 | 处理 |
|---|---|
| `E_FILE_MISSING` | 检查 `sample_data/` 下的文件名与大小写 |
| `E_MISSING_COLUMN` | 表头缺必填列，按列契约补列后重试 |
| `E_DECODE_FAILED` | 文件既非 UTF-8 也非 GB18030，需转码 |
| `Q_ROW_SHORT` / `Q_ROW_LONG` | 该行进隔离区，修正分隔符后重跑 |
| `V_STALE_DECISION` | 页面停留过久，刷新后按新版本再提交 |
| `V_BAD_DECISION` | 税号冲突或编码复用的候选对不允许按名称合并 |
| `V_UNKNOWN_PAIR` | 候选对编号不属于当前批次，刷新页面 |
| `S_STALE_CASE` | 接替案件版本过期，刷新 `/succession` 后按新版本再判定 |
| `S_BAD_VERDICT` | 接替证据不足或动作非法，需补证据或改判另行启用 |
| `S_NOT_APPROVED` | 仍有待判/未批准案件，先逐案判定再发布 |
| `S_PAYLOAD_DRIFT` | 发布计划已变，重新读取计划摘要后重试 |
| `S_STALE_MASTER` | 正式主数据已被其他发布推进，刷新后按新版本重试 |
| `S_PUBLISH_FAILED` | 本次写入已整体回滚；旧版仍在，按需 `restore` 后重试 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
```
