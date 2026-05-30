# 产业星图（Supply Chain Graph）— 产品 & 技术设计文档（PRD + TDD）

> 菜单名：**产业星图**（副标题：基本面关系全景）
> 状态：设计稿 v1（待评审）
> 定位：以图（点-边）形式展示上市公司之间的产业链 / 供应链 / 投资关系，
> 解决用户"探索一家公司或一个行业整体基本面格局"的需求。

---

## 0. 技术决策基线（已拍板）

| 项 | 决策 |
|---|---|
| 图存储 | **复用现有 PostgreSQL**，建 `graph_nodes` / `graph_edges` 表，遍历用递归 CTE（`WITH RECURSIVE`） |
| 前端渲染 | **React Flow（@xyflow/react）为主** 渲染公司视角自定义卡片，**AntV G6** 渲染行业大图力导向 |
| 缓存 | 复用现有 **Redis**，缓存子图查询结果 |
| 录入 | 调研 MD → LLM 解析为规范化三元组 JSON → 校验/去重 → upsert 入库 |
| 菜单名 | **产业星图** |

> 设计原则：schema 保持"图引擎无关"的通用三元组结构，未来若数据规模增长可平滑迁移到
> Apache AGE（Postgres 扩展）或 Neo4j，数据可直接导出。

---

## 1. 功能设计

### 1.1 核心定位
用户从单个公司 / 行业 / 产品出发，向上下游、投资版图、业务分部层层展开，
理解一家公司或一个行业的整体基本面格局。

### 1.2 节点（点）类型

| 类型 | 说明 | 例子 |
|---|---|---|
| `company` 公司 | 上市/未上市实体 | NVDA、TSLA、Anthropic（未上市）、SpaceX |
| `segment` 业务分部 | 公司内部业务板块 | Starlink、xAI、Space 分部 |
| `industry` 行业/产业 | 行业视角聚合 | 卫星通信、商业航天、AI 算力 |
| `product` 产品/部件 | 产品视角穿透枢纽 | 卫星芯片、碳纤维复材、相控阵天线 |

**公司节点关键属性**：`ticker`（可空，未上市为 null）、`name_zh`/`name_en`、
`market`（US/TW/KR/HK/CN，需市场标识+国旗）、`sector`、`is_listed`、`logo_url`、`market_cap`、
可选实时价/IV（复用现有 stock 数据）。

### 1.3 边（关系）类型

| 关系 type | 方向 | 视觉 |
|---|---|---|
| `supplies_to` 供应关系 | A→B（A 供应给 B，B 为客户） | 实线箭头 / 青色 |
| `mutual_supply` 相互供应 | A↔B | 双向箭头 |
| `invests_in` 投资/持股 | A→B（含 equity_pct） | 金色线 |
| `parent_of` 母子/控股 | A→B | 粗实线 |
| `has_segment` 业务分部归属 | 公司→分部 | 树形粗线 |
| `partnership` 合作/合资 JV | A↔B | 虚线 |
| `competitor` 竞争 | A↔B | 红色虚线 |
| `licenses_to` 技术/专利授权 | A→B | 点划线 |
| `manufactures_for` 代工 OEM/ODM | A→B | 实线 |
| `thematic_link` 概念/主题关联 | 无向 | 浅色弱连接 |

**边属性（metadata）**：`direction`(单向/双向)、`weight/strength`(关系强度→线宽)、
`revenue_share`(营收依赖%)、`equity_pct`(持股%)、`confidence`(可信度)、
`evidence`+`source_url`+`as_of_date`(证据/来源/时效，保证可溯源、可回放)。

### 1.4 视角切换（核心交互）

| 视角 | 行为 | 参数 |
|---|---|---|
| 公司视角 | 以公司为中心 ego-network，按业务分部分组，向上下游展开 N 跳 | `focus=ticker&depth` |
| 行业/产业视角 | 某行业内所有公司 + 跨行业供应连接 | `perspective=industry` |
| 产品/部件视角 | 以产品为枢纽穿透所有相关供应商/客户 | `focus=product_id` |
| 供应链视角 | 上游↑/下游↓分层布局 | 分层 layout |

### 1.5 页面交互清单
- 顶部：视角 Tab（公司/行业/产品）+ 实体搜索框（autocomplete）
- 侧栏：关系图例 & 过滤器（每种关系类型独立开关+颜色）
- 深度滑块（1–3 跳）
- 节点卡片：logo + ticker 徽章 + 市场国旗 + 迷你价格 sparkline（复用 recharts）+ 分部配色
- 点击节点 → 右侧详情抽屉（公司简介、关键财务、直接邻居列表、跳转 `/stock/{ticker}`）
- 点击边 → 显示关系证据/来源/时间
- Minimap + 缩放 + 力导向/层级布局切换
- （进阶）时间轴：按 `as_of_date` 回放关系演变

---

## 2. 技术设计

### 2.1 数据模型（PostgreSQL）

**`graph_nodes`**
```
id            uuid / bigint  PK
node_type     enum(company, segment, industry, product)
ticker        text  null      -- 上市公司代码，未上市为 null
market        text  null      -- US/TW/KR/HK/CN
name_zh       text
name_en       text  null
sector        text  null
is_listed     bool
logo_url      text  null
attrs         jsonb           -- market_cap / 实时指标等扩展
created_at, updated_at
UNIQUE(ticker, market) where ticker is not null
INDEX(node_type), INDEX(sector)
```

**`graph_edges`**
```
id            uuid / bigint  PK
source_id     fk -> graph_nodes.id
target_id     fk -> graph_nodes.id
rel_type      enum(supplies_to, mutual_supply, invests_in, parent_of,
                   has_segment, partnership, competitor, licenses_to,
                   manufactures_for, thematic_link)
direction     enum(directed, bidirectional)
weight        float null       -- 关系强度，控制线宽
attrs         jsonb            -- equity_pct / revenue_share / ...
confidence    enum(confirmed, inferred)
evidence      text null
source_url    text null
as_of_date    date
created_at, updated_at
INDEX(source_id), INDEX(target_id), INDEX(rel_type)
```

**`graph_views`**（已策展全景图，便于一键加载，如"SpaceX 全业务供应链"）
```
id, slug, title, perspective, focus_node_id, description, config(jsonb)
```

> 建议同步在 `ontology/objects/company.yaml`、`ontology/relations/supply_chain_links.yaml`
> 沉淀本体定义，保持与现有 ontology 体系一致。

### 2.2 数据录入流水线（你给 MD → 入库）

```
调研 MD
  └(1) LLM 解析：抽取实体+关系 → 符合 schema 的 JSON 三元组（带 source/confidence/as_of_date）
  └(2) 实体消歧/去重：ticker 优先匹配，name 模糊匹配，避免 "英伟达/NVDA" 重复
  └(3) 校验：jsonschema 校验 + 人工/管理后台确认
  └(4) Upsert 入 graph_nodes / graph_edges（幂等）
```
- 管理端接口 `POST /api/v1/graph/ingest`（接收 MD 或已解析 JSON，幂等 upsert，需鉴权）。
- 每条边强制带 `as_of_date` + `source`，保证可溯源、可回放。

### 2.3 后端 API（FastAPI）

```
GET  /api/v1/graph/search?q=spacex          实体自动补全（公司/行业/产品）
GET  /api/v1/graph?focus=SPCX&perspective=company&depth=2&rel_types=supplies_to,invests_in
                                            返回某视角子图 {nodes,edges,meta}
GET  /api/v1/graph/node/{id}                节点详情 + 直接邻居
GET  /api/v1/graph/industries               行业列表（行业视角入口）
GET  /api/v1/graph/views                    已策展全景图列表
GET  /api/v1/graph/views/{slug}             加载某张策展图
POST /api/v1/graph/ingest                   管理端：MD/JSON 入库（鉴权）
```

**统一响应格式**：
```json
{
  "nodes": [{"id":"","type":"company","ticker":"NVDA","label":"英伟达","market":"US",
             "sector":"半导体","segment":"AI","logo":"","metrics":{}}],
  "edges": [{"id":"","source":"","target":"","relType":"supplies_to",
             "direction":"directed","weight":0.8,"attrs":{}}],
  "meta":  {"perspective":"company","focus":"SPCX","depth":2,"asOf":"2026-05-30"}
}
```
- 子图查询走递归 CTE（`WITH RECURSIVE` 从 focus 节点向外 N 跳，按 rel_types 过滤）。
- 结果 Redis 缓存，key = `focus + perspective + depth + filters`。
- 权限接入现有 `nav-visibility` / 鉴权体系，新增菜单 id 走同一套显隐控制。

### 2.4 前端设计（Next.js 15 / React 19 / Tailwind / Zustand）

**路由**：`app/(dashboard)/panorama/page.tsx`
**菜单**：`components/Sidebar.tsx` 的 `NAV_GROUPS` 新增项，id `supply_graph`，
图标 `Network` / `Share2`（lucide），建议新建"基本面"分组或放入"另类数据"。

**渲染库**：
- **React Flow（@xyflow/react）为主**：自定义公司卡片节点（logo+股价+分部配色），
  dagre/层级布局还原 SpaceX 分部树。
- **AntV G6**：行业视角大图，力导向 + 鱼眼 + 聚合。

**组件拆分**：
- `GraphCanvas`：图渲染容器
- `PerspectiveTabs`：公司/行业/产品切换
- `EntitySearch`：搜索 autocomplete（调 `/graph/search`）
- `RelationLegend`：关系类型图例 + 过滤开关（按颜色区分）
- `DepthSlider`：跳数
- `NodeCard`：自定义节点（logo、ticker 徽章、市场国旗、recharts sparkline、分部配色）
- `NodeDetailDrawer`：右侧详情抽屉（跳转 `/stock/{ticker}`）
- `LayoutSwitcher` / `Minimap`
- 状态用 **Zustand** 管理 perspective/focus/过滤器，URL query 同步（便于分享某张图）。

**视觉风格**：沿用现有 glass / primary 玻璃拟态主题（`bg-glass`、`border-glass-border`）；
关系线按类型语义色（投资=金、供应=青、竞争=红）；力导向入场动画；
hover 高亮邻居、淡化无关节点（focus+context）。

---

## 3. 落地里程碑

| 里程碑 | 内容 |
|---|---|
| M1 数据层 | `graph_nodes/edges` 表 + Alembic 迁移 + ingest 接口；SpaceX MD 作为首张种子图入库 |
| M2 后端 API | search + 子图查询（递归 CTE）+ Redis 缓存 |
| M3 前端 MVP | React Flow 渲染公司视角 + SpaceX 全景，自定义节点卡 + 关系图例 |
| M4 视角扩展 | 行业/产品视角 + 过滤器 + 详情抽屉 + 跳转个股页 |
| M5 增强 | 策展图保存、时间轴回放、G6 力导向行业大图 |

---

## 附录 A：SpaceX 全业务供应链（首个种子图样例）

```
SpaceX (SPCX, 核心母体)
 ├─ has_segment → AI 分部 (xAI/Grok/X)
 │    ├─ supplies_to ← NVDA 英伟达 (US)
 │    ├─ supplies_to ← TSLA 特斯拉 (US)
 │    ├─ supplies_to ← INTC 英特尔 (US)
 │    ├─ supplies_to ← Anthropic (未上市)
 │    └─ supplies_to ← Cursor (未上市)
 ├─ has_segment → Connectivity 分部 (Starlink)
 │    ├─ supplies_to ← SATS 回声星 (US)
 │    ├─ supplies_to ← FTC 飞创 (UK)
 │    ├─ supplies_to ← 6285 启碁 (TW)
 │    ├─ supplies_to ← 2313 华通 (TW)
 │    ├─ supplies_to ← 6271 同欣 (TW)
 │    ├─ supplies_to ← STM 意法半导体 (EU)
 │    ├─ supplies_to ← AVGO 博通 (US)
 │    ├─ supplies_to ← TRMB 天宝导航 (US)
 │    └─ supplies_to ← CPSH CPS Tech (US)
 └─ has_segment → Space 分部 (猎鹰/星舰)
      ├─ supplies_to ← HON 霍尼韦尔 (US)
      ├─ supplies_to ← 347700 Sphere (KR)
      ├─ supplies_to ← CRS 卡彭特科技 (US)
      ├─ supplies_to ← MTRN 迈特瑞恩 (US)
      ├─ supplies_to ← HXL 赫氏复合材料 (US)
      ├─ supplies_to ← DCO Ducommun (US)
      ├─ supplies_to ← ATRO 埃斯特罗 (US)
      └─ supplies_to ← LHX L3Harris/Aerojet (US)
```
