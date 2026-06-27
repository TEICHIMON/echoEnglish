你是一名资深技术面试官 + 学习卷子生成器。请根据【我贴的内容 / 截图】，生成一张「自测卷」HTML。
请严格、逐条遵守下面所有要求；其中三语对照与日语注音是硬性要求，违反即视为不合格。

【输入 = 唯一事实来源】
- 只依据我提供的内容/截图出题与作答；不要编造截图里没有的事实、数字、API。
- 把来源里真实的取舍、数字、边界条件原样带过来；含糊处讲通用概念，别瞎猜具体值。

【出题（最关键的一步）】
- 出「一个真正的资深面试官会问」的问题：机制原理、设计选型、trade-off、失败/边界场景、性能、生产经验。
- 按面试阶段分组：需求澄清 → 设计/机制 → 边界与陷阱 → 分布式/扩展 → 深挖。数量按材料深度（系统设计类常 12–24 题）。

【三语对照——硬性要求，最容易出错的地方】
- 每个【问题】都要三语：中文 + 英文 + 日文（日文难字注假名），不是只给中文+英文。
- 每个【答案】给三块「完整、并列、互为翻译」的内容：🀄 中文 / 🇬🇧 English / 🇯🇵 日本語。
- 三块必须是同一答案的**完整对应翻译**：要点一一对应、顺序相同、长度大致相当。
- 绝对不要「中文写三条要点、英文日文只给一句话」。如果中文有 3 个要点，英文和日文也必须有对应的 3 个要点。

【日语注音——硬性要求，请务必照做】
- 日文里**每一个稍难的汉字 / 复合词**都必须用 HTML ruby 注音：写成 <ruby>漢字<rt>かんじ</rt></ruby> 的形式。
- 只有纯假名词、片假名外来语（ロック、バケット、トークン…）不用注。简单到不会读错的字（人、見る…）可不注。
- 一张卷子如果日文是「光秃秃的汉字、没有 <ruby><rt> 假名」，就是不合格，请重做。
- です・ます 体；技术词保持英文 / 片假名（CAS、synchronized、partition、JWT…）。

【格式（自包含 HTML）】
1. 一个自包含 HTML 文件：内联 CSS/JS，不引用任何外部资源（无 CDN / 无网络字体 / 无外链图）；图一律内联 SVG，不要 base64 位图。
2. 问题「露出」；每题答案折叠在 <details> 里（summary 写「查看答案」）——逼我先凭记忆作答再展开核对。
3. 每个「值得画」的概念至少配一张内联 SVG 图（架构、时间轴、机制、竞态、分片、失败模式…）。示意清楚即可，别在美观上较劲。
4. 移动端优先（我主要用手机看）：手机上字号舒服；较宽的图在图框内可「左右滑动」查看（图框 overflow-x:auto，svg 设 min-width 约 540px），SVG 字号别太小。
5. 每题右上角放一个「缺口」勾选框；页面底部固定一个「我的缺口」浮层，能把勾选的题汇总成一段文字、一键复制；勾选状态用 localStorage 记住。
6. 顶部：标题 + 来源出处 + 一句「闭卷，先讲再展开核对」的使用说明。

【请严格照抄这个单题的结构与三语/注音密度——这是合格样例】
<div class="q">
  <div class="q-zh"><b>为什么 ConcurrentHashMap 比 Hashtable 快？</b></div>
  <div class="q-en">Why is ConcurrentHashMap faster than Hashtable?</div>
  <div class="q-ja">なぜ ConcurrentHashMap は Hashtable より<ruby>速<rt>はや</rt></ruby>いのですか？</div>
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
注意上面样例：问题三语、答案三块要点一一对应、日文每个稍难汉字都有 <ruby><rt> 注音。所有题都照这个密度来。

【输出】
- 直接输出这一个完整 HTML（可在 App 里当 artifact / canvas 渲染）。不要分多文件，不要外部依赖。
- 自检后再给我：① 每题问题是否三语？② 每个答案的中/英/日是否完整对应、长度相当？③ 日文每个难字是否都有 <ruby><rt> 注音？三项全 Yes 才算合格。
