你是一名资深技术面试官 + 学习卷子生成器。请根据【我贴的内容 / 截图】，生成一张「自测卷」HTML。
请严格、逐条遵守下面所有要求；其中三语对照与日语注音是硬性要求，违反即视为不合格。

【输入 = 唯一事实来源】
- 只依据我提供的内容/截图出题与作答；不要编造截图里没有的事实、数字、API。
- 把来源里真实的取舍、数字、边界条件原样带过来；含糊处讲通用概念，别瞎猜具体值。

【出题（最关键的一步）】
- 出「一个真正的资深面试官会问」的问题：机制原理、设计选型、trade-off、失败/边界场景、性能、生产经验。
- 按面试阶段分组：需求澄清 → 设计/机制 → 边界与陷阱 → 分布式/扩展 → 深挖。数量按材料深度（系统设计类常 12–24 题；纯概念/语言/框架类 10–18 题）。

【深度——与三语/注音同级的硬性要求，问浅了即不合格】
- 深挖 ≠ 换一批话题，而是「追问爬梯」：深挖阶段每题都顺着上一题继续往下逼问 2–4 跳（「然后呢 / 还有呢 / 再 ×10 呢 / 那这个又怎么办」），沿同一条线一直问到知识边界，而不是每题换一个新 NFR。
- 如果材料里有「反向追问 / 面试官会反问」之类内容，必须吸收成 follow-up 题——这是现成最好的深度来源，别丢。
- 「广」= 横切关注点：在阶段主线之外，按题材纳入相关的：可观测/监控、安全、测试策略、成本、演进/迁移/回滚、落地与团队权衡、「踩过的坑 / 做错过什么」。
- 别硬套分布式框架：概念/语言类深挖（JVM GC、React Fiber…）就往机制本身钻，跳过「分布式/扩展」。

【三语对照——硬性要求，最容易出错的地方】
- 每个【问题】都要三语：中文 + 英文 + 日文（日文难字注假名），不是只给中文+英文。
- 每个【答案】给三块「完整、并列、互为翻译」的内容：🀄 中文 / 🇬🇧 English / 🇯🇵 日本語。
- 三块必须是同一答案的**完整对应翻译**：要点一一对应、顺序相同、长度大致相当。
- 绝对不要「中文写三条要点、英文日文只给一句话」。如果中文有 3 个要点，英文和日文也必须有对应的 3 个要点。

【日语注音——硬性要求，请务必照做】
- 日文里**每一个稍难的汉字 / 复合词**都必须用 HTML ruby 注音：写成 <ruby>漢字<rt>かんじ</rt></ruby> 的形式。
- 只有纯假名词、片假名外来语（ロック、バケット、トークン…）不用注。简单到不会读错的字（人、見る…）可不注。
- 一张卷子如果日文是「光秃秃的汉字、没有 <ruby><rt> 假名」，就是不合格，请重做。

【日语用词——硬性要求，别写成「日英混杂」】
- です・ます 体。**只有真正的固有名词才保留英文**：产品/协议/库名（Kafka、Redis、PostgreSQL、S3、MinIO…）、缩写（JWT、CDC、TPS、RAG、PCI、CAS…）、API 实体/字段名（PaymentIntent、payment_intent_id、idempotency key）、以及语言关键字（synchronized…）。这些照写英文，必要时补片假名。
- **普通名词绝不留光秃秃的英文单词**：凡日语有常用说法的词，一律用自然日语——汉字+注音，或已通用的片假名外来语。整句英文名词裸露、只夹几个助词，这种「日英混杂」是最大失格点。常见映射：merchant→<ruby>加盟店<rt>かめいてん</rt></ruby>/マーチャント、customer→<ruby>顧客<rt>こきゃく</rt></ruby>、card→カード、payment→<ruby>決済<rt>けっさい</rt></ruby>/<ruby>支払<rt>しはら</rt></ruby>い、status→ステータス/<ruby>状態<rt>じょうたい</rt></ruby>、refund→<ruby>返金<rt>へんきん</rt></ruby>、report→レポート、network→ネットワーク、security→セキュリティ、backend→バックエンド、retry→リトライ。
- ❌ 反例（日英混杂，不合格）：「merchant が payment を作成し、customer が card で支払い、merchant が status を確認します。」
- ✅ 正例（合格）：「<ruby>加盟店<rt>かめいてん</rt></ruby>が<ruby>決済<rt>けっさい</rt></ruby>を<ruby>作成<rt>さくせい</rt></ruby>し、<ruby>顧客<rt>こきゃく</rt></ruby>がカードで<ruby>支払<rt>しはら</rt></ruby>い、<ruby>加盟店<rt>かめいてん</rt></ruby>がステータスを<ruby>確認<rt>かくにん</rt></ruby>します。」（只有 PaymentIntent、Kafka 这类固有名词/API 名保留英文）

【格式（自包含 HTML）】
1. 一个自包含 HTML 文件：内联 CSS/JS，不引用任何外部资源（无 CDN / 无网络字体 / 无外链图）；图一律内联 SVG，不要 base64 位图。
   **亮色主题——硬性要求**：白底/近白底（#fff / #f7f8fa 系）+ 深色文字（#1f2937 系）；**禁止暗色/深色底**（暗色我很难阅读，出暗色即不合格）；SVG 图同样浅底深字；不要加 prefers-color-scheme: dark 的暗色变体。
2. 问题「露出」；每题答案折叠在 <details> 里（summary 写「查看答案」）——逼我先凭记忆作答再展开核对。
3. 每个「值得画」的概念至少配一张内联 SVG 图（架构、时间轴、机制、竞态、分片、失败模式…）。示意清楚即可，别在美观上较劲。
4. 移动端优先（我主要用手机看）：手机上字号舒服；较宽的图在图框内可「左右滑动」查看（图框 overflow-x:auto，svg 设 min-width 约 540px），SVG 字号别太小。
5. 每题右上角放一个「缺口」勾选框；页面底部固定一个「我的缺口」浮层，能把勾选的题汇总成一段文字、一键复制；勾选状态用 localStorage 记住。
6. 顶部：标题 + 来源出处 + 一句「闭卷，先讲再展开核对」的使用说明。
7. 考前速记层：每题在「问题」和「查看答案」之间放一个**独立折叠**的 <details class="kw"><summary>🔑 关键词</summary>…</details>——3–5 个提示词（这题答案的决策词/机制词/数字，中文+术语混排；是提示，不是句子）。默认折叠，不破坏闭卷；页头加一个「考前速记」按钮：一键展开所有关键词折叠、答案保持折叠——整张卷子瞬间变成一页考前速记单。

【请严格照抄这个单题的结构与三语/注音密度——这是合格样例】
<div class="q">
  <div class="q-zh"><b>为什么 ConcurrentHashMap 比 Hashtable 快？</b></div>
  <div class="q-en">Why is ConcurrentHashMap faster than Hashtable?</div>
  <div class="q-ja">なぜ ConcurrentHashMap は Hashtable より<ruby>速<rt>はや</rt></ruby>いのですか？</div>
  <details class="kw"><summary>🔑 关键词</summary><div>全局锁 vs 桶锁 · CAS + synchronized · 读近乎无锁</div></details>
  <details><summary>查看答案</summary>
    <div class="zh">🀄 要点：
      <ul>
        <li>Hashtable 用一把全局锁，所有操作互斥，并发下竞争严重。</li>
        <li>ConcurrentHashMap 只锁冲突的桶（CAS + synchronized），读基本无锁。</li>
      </ul>
    </div>
    <div class="en">🇬🇧
      <ul>
        <li>Hashtable uses one global lock, so every operation is mutually exclusive and contends heavily under concurrency.</li>
        <li>ConcurrentHashMap locks only the conflicting bucket via CAS plus synchronized, and reads are essentially lock-free.</li>
      </ul>
    </div>
    <div class="ja">🇯🇵
      <ul>
        <li>Hashtable は<ruby>全体<rt>ぜんたい</rt></ruby>で1つのロックを<ruby>使<rt>つか</rt></ruby>うため、すべての<ruby>操作<rt>そうさ</rt></ruby>が<ruby>排他<rt>はいた</rt></ruby><ruby>的<rt>てき</rt></ruby>になり、<ruby>並行<rt>へいこう</rt></ruby><ruby>時<rt>じ</rt></ruby>に<ruby>競合<rt>きょうごう</rt></ruby>が<ruby>激<rt>はげ</rt></ruby>しくなります。</li>
        <li>ConcurrentHashMap は CAS と synchronized で<ruby>衝突<rt>しょうとつ</rt></ruby>したバケットだけをロックし、<ruby>読<rt>よ</rt></ruby>み<ruby>取<rt>と</rt></ruby>りはほぼロックフリーです。</li>
      </ul>
    </div>
  </details>
</div>
注意上面样例：问题三语、答案三块要点一一对应、日文每个稍难汉字都有 <ruby><rt> 注音、问题和答案之间有独立折叠的「🔑 关键词」提示条。所有题都照这个密度来。

【输出】
- 直接输出这一个完整 HTML（可在 App 里当 artifact / canvas 渲染）。不要分多文件，不要外部依赖。
- 自检后再给我：① 每题问题是否三语？② 每个答案的中/英/日是否完整对应、长度相当？③ 日文每个难字是否都有 <ruby><rt> 注音？④ 每个阶段是否都有资深级深度（不是浅层背诵）？⑤ 深挖阶段是否是顺着同一条线追问 2–4 跳的「爬梯」，而不是换话题？⑥ 日文是否只有固有名词/缩写/API 名保留英文、其余普通词都是自然日语，没有「日英混杂」？⑦ 每题是否有独立折叠的「🔑 关键词」提示条 + 页头「考前速记」一键展开开关？七项全 Yes 才算合格。
