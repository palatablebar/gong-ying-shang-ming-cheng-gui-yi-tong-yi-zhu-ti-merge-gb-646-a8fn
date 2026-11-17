# 供应商主数据台账（entitymerge）

集团采购中心把 6 个 ERP 导出的供应商名录并成一份主数据：名称归一、候选对判定、
合并同主体、人工复核边界对、导出主数据与证据行。界面为原生 HTML/CSS/JS，
单体 FastAPI + Jinja2 + SQLite，一条 `docker compose up` 即可演示。

## 能力

- 采信脏文件：GB18030 与 UTF-8 混用、BOM、重复表头、列数不齐、千分位与全角数字、
  空值哨兵、前导零编码、同号不同值、单边引用、空态三态。
- 名称归一：全角、括号、空白、法律后缀、地名标记、拉丁大小写、标点，共 7 条规则。
- 候选对：归一名的字符 2-gram 倒排桶，超大桶跳过并计数，候选对确定性编号。
- 相似判定：0.55 × bigram Dice + 0.45 × 编辑比率；税号硬冲突拒绝、税号相同强制合并、
  城市不同降级复核、得分分档 0.92 / 0.80。
- 合并簇：并查集合并，代表元取税号最小者，簇内税号冲突标记交复核，
  簇金额与成员分摊用 Decimal 逐分守恒。
- 复核：队列按得分降序，决定带版本乐观锁，写入后簇立即重算；引用异常四类可查。
- 导出：主数据、成员分摊、隔离区、冲突、运行清单（含 `run_id` 与输入 sha256）。
- 编码复用接替：同一编码被注销主体与新主体先后占用时，按证据判主体接替或
  另行启用；余额归属、接替依据、受影响记录在发布前一页看全；发布一次性切换
  正式主数据与新主体记录，失败整体回滚并整版保留旧版。

## 模块清单

| 模块 | 入口 | 规则 |
|---|---|---|
| M1 名录采信 | `POST /api/suppliers/import` | 编码回退链、列数校验、同键冲突归因 |
| M2 名称归一 | `GET /api/normalize` | N01 到 N07 七条规则，行业词不剥离 |
| M3 分块候选 | `POST /api/blocks` | bigram 倒排桶，桶上限 120，候选上限 4000 |
| M4 相似判定 | `POST /api/pairs` | 加权得分与四级优先判定 |
| M5 等价类合并 | `POST /api/clusters` | 并查集、代表元、最大余额分摊 |
| M6 复核批次 | `POST /api/reviews/{pair_id}/decision` | 队列、版本锁、引用一致性 |
| M7 主数据导出 | `GET /api/export/master.csv` | CSV 与运行清单，UTF-8 BOM |
| M8 编码复用接替 | `POST /api/succession/{case_id}/decision`、`POST /api/succession/publish` | 接替证据判定、余额归属、一次性发布与回滚 |

## 页面

`/` 台账总览（含采信诊断与隔离区）、`/normalize` 名称归一、`/pairs` 候选对、
`/clusters` 合并簇、`/reviews` 复核队列、`/succession` 编码复用接替、`/export` 主数据导出。
接口文档在 `/docs`。

## 核心规则

1. 归一名 = 全角转半角 → 括号统一 → 去空白 → 去法律后缀 → 去地名标记 → 拉丁小写 → 去标点。
2. 得分 = 0.55 × bigram Dice + 0.45 × 编辑比率，量化到 6 位小数。
3. 判定优先级：编码复用（拒绝） > 税号冲突（拒绝） > 税号相同（自动） > 城市不同（复核） > 得分分档。
4. 代表元 = 簇内税号最小者；无税号取供应商号最小者。
5. 簇金额 = Σ成员金额（Decimal）；成员分摊按比例取整到分后最大余额补齐。
6. 同一集团编码下出现不同记录号且税号不同 = 编码复用：先经 M8 接替复核，
   候选对判 `P_CODE_REUSE` 拒绝，簇不得把旧新主体按名称并到一起。
7. 接替判定：税号相同属同一主体补录；旧主体已注销且有名称一致或接替标记
   （证据 ≥2）才判主体接替；否则新主体另行启用、余额留在旧主体，旧主体已注销
   的同步退役，仍存续的保留可结算状态。
8. 发布：批准全部案件后一次性切换正式主数据与新主体记录，计划摘要与旧版版本
   双重校验；同一批重跑只回放原回执，失败单事务回滚并整版保留旧版。

字段类型、单位、空值与哨兵、时间与区间定义见 `docs/DATA-DICTIONARY.md`；
陷阱清单与对账规则见 `docs/DEPTH.md`；运维动作见 `docs/RUNBOOK.md`。

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
docker compose exec -T app python -m pytest -q
```

宿主机访问 <http://127.0.0.1:18205>。样例数据在 `sample_data/`，六个文件分别覆盖
主档、别名、GB18030、金额与引用、仅表头、全空行；另有 `succession_g_lifecycle.csv`
演示编码复用（含主体状态与未结余额），只在接替页点“载入生命周期样例”时并入。

## 演示步骤

1. 打开 `/`：看到批次号、采信行数、主体数与对账结论。
2. `/` 下方：逐文件诊断与隔离区两行带原因码与原始行号。
3. `/normalize`：原名到归一名的对照，试算框可现场验证一条名称。
4. `/pairs`：按结论过滤候选对，看得分与依据字段。
5. `/clusters`：合并簇与成员分摊，`C-0009` 标税号冲突。
6. `/reviews`：三项带复核，判定后簇数立即变化；引用异常五条。
7. `/succession`：点“载入生命周期样例”，看到四类编码复用——注销+同名判接替、
   同名但旧主体存续只判疑似、注销但无依据另行启用、承继标记+注销判接替；
   每案列出余额归属、接替依据与受影响记录。
8. 逐案判定后，“一次性切换发布”同时落正式主数据与新主体记录；回执、旧版快照
   与“恢复此版”在同一页可查。

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

110 条测试：每个模块 8 到 13 条，另有编码复用 29 条、端到端 4 条与不变量 5 条。

## 目录

```text
app/main.py config.py errors.py reasons.py manifest.py store.py web.py
app/modules/m1_intake … m7_export/{handler.py service.py 规则与读写出}
app/modules/m8_succession/{classify.py detect.py service.py handler.py}
app/templates/{base.html + 7 页}
app/static/{theme.css layout.css components.css app.js table.js charts.js}
docs/{DEPTH.md DATA-DICTIONARY.md RUNBOOK.md}
sample_data/ 六个脏样例 + 编码复用生命周期样例
tests/{conftest.py test_intake … test_succession.py test_e2e.py test_invariants.py fixtures/}
```
