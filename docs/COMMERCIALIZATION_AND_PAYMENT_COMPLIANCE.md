# OptionsAji 商业化与支付合规改造报告

> 面向 Stripe / Creem 接入审核与「极客工具 → 商业 SaaS」转型的改造蓝图。
> 本文为**设计与合规指引文档**，不含代码实现，供据此自行开发。
> 适用仓库：`options-aji`（前端）、`options-aji-backend`（后端）。
> 最后更新：2026-06-06

---

## 0. 现状速判（基于代码盘点）

你不是「产品不规范」，而是**法务外壳与部分营销措辞不规范**。技术与计费链路已基本就绪，缺口集中在合规法务页、信任表达、以及几处会触发金融类风控的措辞。

| 维度 | 现状 | 结论 |
|---|---|---|
| Landing Page | 已有 `options-aji/app/landing/page.tsx`，含 Hero/功能/定价/证言/Footer | ✅ 骨架够，需补合规与去夸大 |
| 定价方案 | Free / Pro $49 / Alpha $149 三档 | ✅ 结构合理 |
| 支付集成 | Stripe checkout + portal + webhook 全链路（`app/api/routes/billing.py`） | ✅ 技术就绪 |
| 用量计费 | 免费 20 次/日，Pro 无限，`UsageDailyRow` 计量；超额返回 HTTP 402 | ✅ 已有 |
| **分层内容门控** | 新增 `guest / trial / pro` 三层（`app/services/mvp_entitlement.py` + `resolve_mvp_entitlement`），按层裁剪 API 响应 | ✅ 漏斗清晰，但权益源需收敛（见 §3.3） |
| **服务条款 / 隐私政策** | Footer 链接为 `href="#"` 占位，**无真实页面** | ❌ **硬伤** |
| **退款政策** | 全站无 | ❌ **硬伤** |
| **联系方式 / 公司主体** | 无 About/Contact | ❌ **硬伤** |
| 风险免责 | 有「不构成投资建议」，但仅 Footer 一处灰字 | ⚠️ 不够显著 |
| 营销话术 | 「98.7% 准确率」「像专业机构一样交易」 | ❌ **高风险措辞** |

**一句话**：补齐 4 个法务页 + 修 2 处话术 + 收敛权益源，过审概率大幅提升。

---

## 1. 合规准入检查表（Stripe / Creem 硬性要求）

### 1.1 通用硬性门槛（两家共有）

支付平台审核时，机器人 + 人工会逐项核对以下要素是否在**站内公开可达**（不能藏在登录墙后）：

| 必备要素 | 要求细则 | 当前缺口 |
|---|---|---|
| 服务条款 (Terms of Service) | 独立 URL，说明服务范围、用户义务、责任限制、争议解决 | 占位链接 → 需建 `/terms` |
| 隐私政策 (Privacy Policy) | 收集哪些数据、用途、第三方共享（含 Stripe/Creem/邮件服务）、Cookie、删除权 | 占位链接 → 需建 `/privacy` |
| 退款政策 (Refund/Cancellation) | 数字订阅必须明示：能否退款、是否按比例、如何取消、生效时间 | 完全缺失 → 需建 `/refund` |
| 联系方式 | 可达 email（建议 `support@域名`）+ 主体名称，部分场景需地址 | 缺失 → 需建 `/contact` 或 Footer |
| 明确的产品说明 | 付费前能清楚知道「买什么、如何交付、是否实时」 | 部分有，需在定价页强化 |
| 定价透明 | 价格、币种、计费周期、自动续费、是否含税，**结账前可见** | 基本有 |
| 业务与收款一致 | 网站描述业务 = 支付账户填写的业务类别 = 收款描述符 | 需对齐填写 |

### 1.2 Stripe 针对「金融 / 数据类」的特殊敏感点

Stripe 对 investment / trading / financial advice 类目极其敏感（属 Restricted Businesses 灰区）。重点：

1. **绝不能表现为「投资建议 / 荐股 / 代客理财」**。定位为 **数据分析与教育工具（data analytics & education）**，而非 trading signals / 投顾。
2. **不得承诺收益或暗示胜率**。「准确率 98.7%」「稳赚」「跑赢大盘」均为风控关键词。
3. **风险免责需显著**，不能只在 Footer 灰字。
4. **强调「不接触资金」**：在条款中写明「本平台不执行交易、不接触用户资金、不连接券商账户」——你确实如此，这是降低风险评级的关键优势。
5. **监管措辞**：面向美国用户提及美股期权时，写明「非注册投资顾问（not a registered investment adviser）」，监管语境为 SEC/FINRA。

### 1.3 Creem 的特点（与 Stripe 的差异）

- Creem 是 **Merchant of Record (MoR)**，替你承担全球税务/VAT 与部分合规，对独立开发者 / AI SaaS 更友好，审核相对宽松。
- 但 Creem 同样在禁止/受限名单中列有 financial / investment advisory 类目。准入逻辑一致：**定位成工具 + 教育，而非投顾**。
- MoR 模式下退款由 Creem 处理，因此你的**退款政策必须与 Creem 默认条款兼容**（通常支持一定期限退款），并在站内同步声明。
- **建议策略**：Creem 作为上手快、容忍度高的主用/备用收款；Stripe 作为长期主力。两者要求高度重叠，一次性补齐法务页即可双投。

---

## 2. 产品信任度缺失分析（「极客味」为何被拒 / 不被信任）

支付风控与真实用户的「不信任」往往源自同一批信号。

### 2.1 触发风控拒批的元素

1. **占位 / 死链法务页**（`href="#"`）——审核机器直接判定「无 ToS/Privacy」，常见一票否决。
2. **夸大业绩表述**——「98.7% 准确率」「机构级」「稳赚」在金融语境=红旗。
3. **无可达联系方式**——无法验证主体真实性。
4. **域名 / 品牌 / 收款描述符不一致**——支付账户填的主体名 ≠ 网站品牌 ≠ 收款描述。
5. **登录墙过重**——核心说明都藏在登录后，审核者看不到你卖什么。
6. **匿名感**——无 About、无主体信息，像一夜搭起来的站。

### 2.2 让真实用户不信任的「实验感」元素

1. **术语裸奔**：GEX / Net GEX / Gamma Flip 直接糊脸，无 onboarding 与解释层。
2. **半成品状态裸露**：「未知」「VIX 数据缺失」「等待利率数据」「阿吉深度洞察暂不可用」等 fallback 文案直接暴露给付费用户——信任杀手。
3. **视觉不一致**：多种强调色（金/黄/红/紫/青）、信息块密度不均，像内部工具。
4. **社会证明不可信**：证言若无真实身份反而减分。
5. **缺「确定性」表达**：金融工具的信任=确定性。缺少数据来源标注、更新频率承诺、退款保障等兜底信号。

---

## 3. 商业化转型设计方案

### 3.1 页面架构建议（Landing Page 模块清单）

已有 Hero/功能/定价/证言/Footer，按标准 SaaS 顺序补全与重排：

```
1. 顶部导航      Logo｜功能｜定价｜文档｜登录/注册 (CTA)
2. Hero          价值主张(1句) + 副文案 + 主CTA + 产品截图
                 ⚠️ 改为工具/效率导向，去掉暗示收益的话术
3. 信任条        "数据来源: FMP / OpenBB / Futu 等" + "不接触资金/不下单" + 用户数/数据点
4. 核心功能      3-5 个，每个=图标+标题+一句利益点 (GEX/AI分析/扫描器/财报/宏观)
5. 工作原理      3 步走 (连接数据 → AI 分析 → 你决策)，降低黑箱感
6. 用例/角色     "适合谁": 期权交易者 / 学习者 / 量化爱好者
7. 定价方案      Free/Pro/Alpha 三卡 + 功能对比表 + 自动续费/可取消说明 + 币种
8. FAQ           见下方必含清单
9. 风险免责区    独立板块(非Footer灰字)：教育用途/非投顾/有风险
10. 最终 CTA     注册/开始试用
11. Footer       产品 | 公司(About/Contact) | 法务(Terms/Privacy/Refund) | 社交
```

**FAQ 必含项（兼顾用户 + 风控）**：

- 这是投资建议吗？→ 明确「否，仅数据与教育」
- 你们会碰我的钱 / 券商账户吗？→「否，不下单不托管」
- 数据来源与延迟？→ 列明来源、免费档延迟说明
- 如何取消订阅 / 能否退款？→ 指向退款政策
- 数据多久更新？支持哪些标的？
- 支付安全吗？→「由 Stripe/Creem 处理，我们不存储卡号」

**必须新建的 4 个法务 / 信息页（替换 `href="#"`）**：

- `/terms` 服务条款
- `/privacy` 隐私政策（须列出 Stripe/Creem、邮件服务、数据分析等第三方）
- `/refund` 退款与取消政策
- `/contact`（或 `/about`，含联系方式与主体信息）

> 这些页面须**登录前公开可达**，并在结账流程、注册页也放置可见链接。

### 3.2 UI/UX 规范化建议（从「实验感」到「企业感」）

1. **设计令牌收敛**：1 个主品牌色 + 1 个强调色，统一圆角、间距、字阶。一致性本身=企业感。
2. **消除半成品 fallback 暴露**：将「未知 / 数据缺失 / 暂不可用」替换为优雅空状态 / 骨架屏，对付费功能做降级提示而非裸露报错。**当前最伤信任的一点。**
3. **加 onboarding 层**：首次进入给 3 步引导 + 术语 tooltip（GEX/Gamma Flip 悬浮解释），把专家工具包装成「专家级但可上手」。
4. **数据可信标注**：每个数据卡角标注来源与更新时间（「来源 FMP · 5 分钟前」）。确定性=信任。
5. **专业排版**：统一图表风格、对齐网格、留白；减少高饱和霓虹色块，金融 SaaS 偏向克制深色 + 高对比文字 + 节制的强调色。
6. **品牌一致性**：Logo、产品名 OptionsAji、收款描述符、法务主体名四者统一。
7. **风险/免责的视觉语言**：用专门提示组件（非灰字），传达「我们很专业地告诉你风险」，反而增信。

### 3.3 商业流程优化（后端封装到 SaaS 标准）

后端已具备 Stripe webhook、`ApiEntitlementRow`、`UsageDailyRow`、`LlmUsageRow`、角色系统，以及新增的 `guest/trial/pro` 内容门控。打磨重点：

1. **⚠️ 权益源收敛（最高优先架构问题）**：当前存在**三套并行的「pro」定义**，口径会冲突：
   - Stripe 订阅 → `ApiEntitlementRow.plan = "pro"`（用于 AI agent 计费 + 每日额度）
   - Access Key → `resolve_mvp_entitlement` 返回 `tier = "pro"`（用于 `mvp_entitlement.py` 内容裁剪）
   - JWT `role = "admin"` → 直接 bypass 为 pro
   
   **建议**：以 **Stripe 订阅作为唯一商业计费真实源**；将 `AccessKeyRow`（trial/paid + 设备绑定）退为内部 / 企业白名单 / 线下试用用途；统一一个「用户当前权益等级」解析函数，让 `mvp_entitlement` 的 tier 与 Stripe plan 对齐，避免「付了 Stripe 却仍被按 guest/trial 裁剪」的信任崩塌。

2. **订阅状态单一可信源**：以 Stripe webhook 为准（已处理 `checkout.session.completed` / `subscription.updated` / `subscription.deleted` + 事件去重 `StripeWebhookEventRow`）。前端 `billing/status` 永远读后端实体而非缓存。

3. **计费失败 / 逾期处理**：补 `invoice.payment_failed`、`customer.subscription.past_due` 的优雅降级（宽限期 + 站内提醒），而非直接断服。

4. **退款 / 取消可自助**：已有 Stripe customer portal 入口，确保用户能自助取消——过审与口碑双赢，并与 `/refund` 文案一致。

5. **用量透明**：把 `UsageDailyRow` 额度在用户中心可视化（「今日 7/20 次 AI 问答」）；免费档触顶（已有 402）配清晰升级引导文案。

6. **可观测性即专业度**：`LlmUsageRow` 成本追踪已有——再加 webhook 幂等、重试、对账日志，构成商业 SaaS 后台标配。

7. **数据合规**：隐私政策声明的「用户数据删除权」要有对应后端删除流程（GDPR/CCPA 友好），尤其面向美/欧用户。

8. **内容门控措辞复核**：`mvp_entitlement.py` 裁剪的字段含 `trade_implications_zh` / `scenario_zh` / `risk_watch_zh`。「交易含义」等措辞偏向投资建议，建议改为「情景推演 / 教育性解读 / 数据观察」等中性表达，与「非投顾」定位一致（详见 §4）。

---

## 4. 避坑指南：金融科技申请支付接口最易被拒的「红线」

> 按对当前代码的杀伤力排序。

1. **业绩 / 收益承诺**——「98.7% 准确率」「像专业机构一样交易」及任何暗示赢率或回报的话术。**立刻删改**为能力 / 效率描述。金融类被拒第一红线。

2. **法务页缺失或死链**——`href="#"` 的 ToS/Privacy、零退款政策。审核机器直接判失败。**上线前必补 4 个页面。**

3. **定位成投顾 / 荐股 / 信号服务**——名称或字段含 `signals` / 「交易计划」/ 「荐股」/「代客」会被归入受限类目。全站口径统一为 **data & education, not investment advice, not a registered adviser, 不接触资金、不代下单**。

4. **主体信息不一致或缺失**——支付账户主体名 / 网站品牌 / 收款描述符 / 联系邮箱四者不一致或查无此人。补真实可达的 `support@` 邮箱与主体名称。

5. **免责声明形同虚设**——只在 Footer 灰字一行。需在结账页、风险板块、FAQ 多点显著呈现「交易有风险、教育用途、自负盈亏」。

---

## 5. 落地优先级（建议两周内）

### P0 — 过审前置（必做）

- [ ] 新建 `/terms` `/privacy` `/refund` `/contact`，替换 Footer `href="#"`
- [ ] 删除 / 改写「98.7% 准确率」「像专业机构一样交易」等业绩话术
- [ ] 全站定位统一为「数据分析 + 教育，非投顾，不接触资金」；风险免责升级为显著板块
- [ ] 配置真实 `support@域名` 邮箱 + 主体名称，四处口径一致

### P1 — 信任与体验

- [ ] 消除「未知 / 数据缺失 / 暂不可用」裸露，改空状态 / 骨架屏
- [ ] FAQ 板块 + 数据来源标注 + 用量可视化
- [ ] design tokens 收敛，统一品牌色与排版

### P2 — 后端商业化打磨

- [ ] **权益源收敛**：统一 Stripe plan 与 `mvp_entitlement` tier，AccessKey 退为内部用途
- [ ] 补 `payment_failed` / `past_due` 优雅降级
- [ ] 持续复核事件解读字段，避免出现「交易计划」等投顾化措辞
- [ ] 数据删除流程对齐隐私政策

---

## 附录：关键文件索引

| 主题 | 文件 |
|---|---|
| Stripe 计费（checkout/portal/webhook） | `app/api/routes/billing.py` |
| 计费实体 / 用量 / LLM 成本 | `app/db/models.py`（`ApiEntitlementRow` / `UsageDailyRow` / `LlmUsageRow` / `StripeWebhookEventRow`） |
| 用户与角色 | `app/db/models_user.py`（`UserRow.role`: user/admin/disabled） |
| Access Key（trial/paid + 设备绑定） | `app/db/models.py`（`AccessKeyRow`）、`app/api/routes/access_keys.py` |
| guest/trial/pro 内容门控 | `app/services/mvp_entitlement.py`、`app/api/deps_access_key.py`（`resolve_mvp_entitlement`） |
| 配置项 | `app/config.py`、`settings.example.toml`（`mvp_trial_enabled`、`stripe_*`、`free_tier_daily_agent_queries`） |
| 前端 Landing / 定价 | `options-aji/app/landing/page.tsx` |
| 前端计费代理路由 | `options-aji/app/api/billing/{checkout,status,portal}/route.ts` |

> 本文件仅为产品与合规设计指引，不构成法律意见；正式上线前请就具体司法辖区的金融信息服务与消费者保护要求咨询专业律师。
