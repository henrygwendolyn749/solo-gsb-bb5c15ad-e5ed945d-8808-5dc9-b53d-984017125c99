# Address Matcher · 标准地址匹配 Web 应用

一个基于 **FastAPI + SQLite** 的本地 Web 应用，把杂乱的外部地址文本匹配到用户上传的
**标准地址表**。内置中文与英文地址处理，自动统一 Unicode、大小写、常见道路缩写与门牌号
格式，按 **词元相似度** 与 **门牌号相似度** 两条轴线分别打分；最高分过低或与次高分差距
过小的候选会被标记为 **待确认（needs_review）**，并在界面上展示命中词元、冲突字段与
可复核结果。

- 🌐 网页界面：单条/批量匹配、上传标准地址 CSV、待确认队列人工复核（含同案合并复核）
- 🔌 REST API：`/api/match`、`/api/match/batch`、`/api/addresses`、复核接口（含 `/docs` Swagger）
- 🗄️ SQLite 持久化，零外部依赖，数据通过 Docker volume 保存
- 🌍 归一化：NFKC + 变音符折叠 + 繁简中文映射 + 全角字符、道路缩写（St/Rd/Ave…）、门牌号（`#12-B`/`No.12b`/`12号`）
- 👥 同案合并复核：归一化后道路/地区词元与门牌号一致、且候选（ID/顺序/得分）完全相同的待确认记录自动归组，可整组确认首选、改派共同候选或判无匹配；整组写入在单个 SQLite 写事务内完成，组成员或签名有变化即整体冲突回滚

---

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

启动后打开：

| 入口 | 地址 |
| --- | --- |
| 网页界面 | http://localhost:8000 |
| REST API 文档（Swagger） | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/health |

- **端口**：`8000`（在 `docker-compose.yml` 的 `ports` 中修改）
- 首次启动会自动加载内置示例标准地址（`sample_data/standard_addresses.csv`，共 15 条，含中英文地址）
- SQLite 数据库保存在 Docker volume `address-data`（容器内 `/data/addresses.db`），重启不丢失

不用 Docker 时：

```bash
pip install -r requirements.txt
ADDRESS_DB_PATH=./data/addresses.db \
SAMPLE_CSV=./sample_data/standard_addresses.csv \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 快速体验

网页打开 http://localhost:8000 ，点击 **「填充示例」** 再点 **「开始匹配」**，
或直接用 curl：

```bash
# 1) 自动命中：缩写 + 大小写被统一（123 Main St. -> 123 Main Street）
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "123 main st., New York, NY 10001"}'

# 2) 待确认（门牌号冲突）：街道词元完全命中，但门牌号 1010 ≠ 1000/100
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "1010 Lake Dr, Austin TX"}'

# 3) 待确认（歧义）：50 / 52 Hillcrest Rd 且门牌号缺失，前两名分差为 0
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "Hillcrest Road, Dallas"}'

# 4) 自动命中（中文 + 全角数字 + 繁体字）：北京東路８８號 3楼
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "北京東路８８號 3楼, 上海"}'

# 5) 自动命中（Tce -> Terrace 缩写归一 + 仅街道无城市也不扣分）
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "742 Evergreen Tce"}'

# 6) 无匹配
curl -s -X POST http://localhost:8000/api/match \
  -H 'Content-Type: application/json' \
  -d '{"query": "1 Milky Way, Mars Base"}'
```

批量匹配：

```bash
curl -s -X POST http://localhost:8000/api/match/batch \
  -H 'Content-Type: application/json' \
  -d '{"queries": ["123 main st ny", "88 Beijing East Rd Shanghai", "xxxxx"]}'
```

上传自己的标准地址表（直接上传，服务端解析后立即写入并跳过重复）：

```bash
curl -s -X POST http://localhost:8000/api/addresses/upload \
  -F 'file=@sample_data/standard_addresses.csv'
```

**上传前预览确认（推荐）**：先预览有效/重复/无效数量与无效行号，确认时再
**重新提交同一文件**。服务端会在同一个 SQLite 写事务内核对预览凭证绑定的
文件 SHA-256 摘要与地址表快照，任一不符都不会写入，必须重新预览。

```bash
# 1) 预览：不写入任何记录，返回 preview_token 与统计
curl -s -X POST http://localhost:8000/api/addresses/preview \
  -F 'file=@sample_data/standard_addresses.csv'
# => {"preview_token":"...","valid":15,"duplicates":0,"invalid":0,
#     "invalid_lines":[],"total":15,"filename":"..."}

# 2) 确认：必须再次提交文件，并带上预览凭证
curl -s -X POST http://localhost:8000/api/addresses/confirm \
  -F 'file=@sample_data/standard_addresses.csv' \
  -F 'preview_token=上一步返回的 token'
# => {"inserted":15,"duplicates":0,"invalid":0,"invalid_lines":[],...}
```

- 预览只写入一条带 30 分钟有效期的凭证（含文件摘要、地址表快照、有效行内容），
  **不写地址表**；
- 确认时若文件被改动（摘要不符）、地址表在预览后发生变化（快照不符）、
  凭证不存在或已过期，接口分别返回 `409` / `409` / `404`，整笔事务回滚，
  需要重新调用预览接口；
- 确认成功后凭证立即失效（一次性），且仅写入该次预览中分类为「有效」的记录；
- 「标准地址表」页默认勾选「上传前先预览确认」；取消勾选则继续使用直接上传接口。

CSV 支持的列名（均可识别别名，表头大小写不敏感）：

| 标准列 | 可识别别名 |
| --- | --- |
| `address` | `full_address`、`formatted_address`、`addr` |
| `line1` | `street`、`address_line1` |
| `city` | `town` |
| `state` | `province`、`region` |
| `postal_code` | `zip`、`zipcode`、`postcode` |
| `country` | — |
| `external_id` | `id`、`code` |

没有 `address` 列时会自动用 `line1, city, state, postal_code, country` 拼接。

---

## 匹配逻辑说明

1. **归一化**（`app/normalization.py`）
   - Unicode NFKC（全角字母/数字→半角）、变音符折叠（`Müller`→`muller`）、
     常用繁体字→简体字；
   - 统一小写、去除多余标点；
   - 道路缩写归一：`street/str/st.`→`st`、`road/rd.`→`rd`、`avenue/av`→`ave` 等，
     方向词 `north/south/east/west`→`n/s/e/w`；
   - 门牌号单独抽取并规范化：`#12-B`、`No. 12b`、`12号`、`１２Ｂ` → `12b`
     （5 位孤立数字识别为邮编，`Apt/Suite/Unit/栋/室` 后的数字不会误当门牌号）。

2. **词元化与词元相似度**（`app/matcher.py`）
   - 地址先按逗号/结构化字段拆成 **道路行**（street）与 **地区**（city/state/postcode）两个部分；
   - 英文按词切分；中文切单字并叠加相邻二元组（bigram），兼容轻微表述差异；
   - 以「查询词元 ↔ 候选词元」贪心配对（完全相等优先，其次编辑距离比率 ≥ 0.72，
     或 3 字符以上的一方包含另一方；单字/短缩写必须精确相等，避免 `ter` 误命中 `water`），
     分别计算道路与地区的 **精确率 / 召回率 / F1**；
   - 查询未提供地区时，地区部分不参与扣分（只写街道名的查询仍是完整输入）；
     查询提供了地区则按 道路 0.75 / 地区 0.25 加权；
   - 结果中输出每个命中词元及其相似度，查询独有、候选独有的词元分别标出，
     地区冲突（如查询写了城市但候选缺失/不一致）单列在 `conflicts` 中。

3. **门牌号相似度**
   - 完全相同 = 1.0；数字相同仅字母不同（`12b` vs `12`）= 0.7；
   - 数字不同按数值接近度给**极低**的部分分（上限约 0.3，门牌号错了通常就是另一处房产）；
   - 两边都没有门牌号 = 0.5（中性），仅一方有 = 0.15。

4. **综合得分与判定**
   - `score = TOKEN_WEIGHT × 词元F1 + (1−TOKEN_WEIGHT) × 门牌号分`（默认权重 0.7 / 0.3）
   - `score ≥ 0.80` 且领先第二名 ≥ `0.06` → **自动命中 `auto_matched`**
   - `0.35 ≤ score < 0.80`，或前两名分差 < `0.06` → **待确认 `needs_review`**
   - `score < 0.35` → **无匹配 `no_match`**

   阈值均可通过环境变量调整：`AUTO_THRESHOLD`、`MARGIN_THRESHOLD`、
   `REJECT_THRESHOLD`、`TOKEN_WEIGHT`。

5. **人工复核**：待确认记录可在网页「待确认队列」或通过
   `POST /api/matches/{id}/review` 执行 `confirmed`（确认首选）、
   `overridden`（改派为其他候选，带 `selected_id`）、`rejected`（标记无匹配）。

6. **同案合并复核**（`app/review_groups.py`）
   - **归组规则**：待确认记录按查询归一化后的 **道路词元 + 地区词元 + 门牌号**
     归组；同一归一化桶内，只有 **候选地址 ID、顺序及得分完全相同** 的记录才进入
     同组（组签名 = 归一化键 + 有序 `[候选ID, 得分]` 序列的 SHA-256）。匹配是确定性的，
     因此同一标准地址表下写法不同的同案查询自然同组；若两次匹配之间标准地址表
     发生变化导致候选不同，则自动拆分为不同组，不会误并。
   - **网页展示**：「待确认队列」页顶部列出成员数 ≥ 2 的同案组，展示组内
     **原始查询及数量**、归一化键与共同候选；原有单条复核在下方列表中保留。
   - **整组复核**：`GET /api/review-groups` 查看分组，
     `POST /api/review-groups/review` 携带 `group_key` 与客户端看到的
     `match_ids` 执行整组 `confirmed` / `overridden`（改派共同候选，带
     `selected_id`，必须是组内共同候选之一）/ `rejected`。
   - **事务保证**：服务端在 **同一个 SQLite 写事务**（`BEGIN IMMEDIATE`）内
     重新归组并核对：每位成员仍为待确认、组签名与成员集合均未变化，才逐条写入
     复核决定与对应得分（确认首选记各成员自己的首选及得分，改派记共同候选的得分，
     判无匹配清空选中与得分）；任一核对失败即返回 `409` 冲突且 **一条也不写**，
     客户端刷新后重试。

```bash
# 查看同案分组（仅含待确认记录）
curl -s http://localhost:8000/api/review-groups

# 整组确认首选 / 改派共同候选 / 判无匹配
curl -s -X POST http://localhost:8000/api/review-groups/review \
  -H 'Content-Type: application/json' \
  -d '{"group_key": "<上一步返回的 group_key>", "match_ids": [1,2,3],
       "action": "confirmed"}'
```

---

## API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 + 标准地址/待确认计数 |
| GET | `/api/addresses` | 列出标准地址 |
| POST | `/api/addresses` | JSON 批量写入标准地址 |
| POST | `/api/addresses/upload` | 上传 CSV（multipart，直接写入，跳过重复） |
| POST | `/api/addresses/preview` | 上传 CSV 生成预览凭证：有效/重复/无效数量 + 无效行号（不写入） |
| POST | `/api/addresses/confirm` | 重新提交文件 + `preview_token`，事务内校验摘要/快照后仅写入有效记录 |
| POST | `/api/addresses/load-sample` | 重新加载内置示例数据 |
| POST | `/api/match` | 单条地址匹配 |
| POST | `/api/match/batch` | 批量地址匹配 |
| GET | `/api/matches?status=needs_review` | 匹配记录/待确认队列 |
| GET | `/api/matches/{id}` | 单条匹配详情 |
| POST | `/api/matches/{id}/review` | 单条复核：confirmed / overridden / rejected |
| GET | `/api/review-groups` | 待确认记录的同案分组（归一化键 + 候选签名归组，含组内原始查询与数量） |
| POST | `/api/review-groups/review` | 整组复核：单事务核对组签名与成员集合，冲突返回 409 且一条不写 |

匹配结果示例（节选）：

```json
{
  "query": "1010 Lake Dr, Austin TX",
  "status": "needs_review",
  "reason": "最高得分低于自动确认阈值",
  "best_id": 5,
  "best_score": 0.786,
  "runner_up_id": 4,
  "runner_up_score": 0.7,
  "candidates": [
    {
      "address": {"id": 5, "full_text": "1000 Lake Drive"},
      "score": 0.786,
      "street_f1": 1.0,
      "locality_f1": 1.0,
      "house_score": 0.288,
      "matched_tokens": [{"query": "lake", "candidate": "lake", "similarity": 1.0}],
      "conflicts": [
        {"field": "house_number", "query": "1010", "candidate": "1000",
         "similarity": 0.288, "message": "门牌号不一致"}
      ]
    }
  ]
}
```

---

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由（网页 + REST API）
│   ├── config.py          # 阈值/路径等环境变量配置
│   ├── db.py              # SQLite 表结构与读写
│   ├── normalization.py   # Unicode/缩写/门牌号归一化与分词
│   ├── matcher.py         # 词元与门牌号相似度、候选判定
│   ├── review_groups.py   # 同案合并复核：归组与整组事务写入
│   ├── ingest.py          # CSV / JSON 标准地址导入
│   ├── schemas.py         # Pydantic 模型
│   ├── templates/         # 网页（Jinja2，单页）
│   └── static/            # 前端 CSS / JS（原生 JS，无构建步骤）
├── sample_data/
│   └── standard_addresses.csv
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt
python -m pytest -q
```
