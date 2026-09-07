Create a mock interview script for language shadowing practice.

Target language: English
Native translation language: Chinese

Output format must be exactly:
Q:<one short interviewer question>|||<Chinese translation aligned to the English>
A:<one short answer chunk>|||<Chinese translation aligned to the English>
A:<optional next short answer chunk>|||<Chinese translation aligned to the English>

Rules:
- Use only Q: and A: prefixes.
- Put one Q or A per line.
- Put 1-5 A lines after each Q.
- Use 1-2 A lines for a simple answer. Use 3-5 A lines when a complex answer needs a mechanism, trade-off, boundary, or example.
- Each A line must contain one spoken sentence with one main idea. Keep it independently understandable.
- English A lines should normally contain 10-16 words and never exceed 18 words. A simple idea may be shorter.
- A sentence may contain one simple cause, condition, time, or contrast clause, using common connectors such as because, if, when, while, or although.
- Do not use nested clauses or long relative clauses.
- Split long answers into ordered A lines: conclusion, reason or mechanism, trade-off, then boundary or example.
- Keep each question natural and interview-like. It may contain one simple clause when needed, but no nested clauses.
- Keep each answer realistic, clear, concise, and spoken. Make the candidate sound confident but not over-polished.
- Prefer common, natural, easy-to-say connectors. Connector examples are guidance, not a strict whitelist.
- Use practical workplace vocabulary.
- Preserve technical terms like replay, backfill, API, cache, database, deployment, observability, and incident response when appropriate.
- Avoid idioms, rhetorical flourishes, and inflated words such as spearheaded, leveraged, mission-critical, cutting-edge, world-class, or seamlessly.
- Term consistency: use one word for one concept throughout the whole script, at most two, and never rotate synonyms to avoid repetition. Native speakers repeat. For example always use (not utilize / employ), keep (not retain / preserve / maintain), improve (not enhance / refine), start (not initiate / launch).
- Non-technical vocabulary stays within the 2000 most common English words (NGSL). Technical terms and everyday engineering words are exempt, but each of them also gets one spelling throughout.
- Before output, list the verbs and nouns you used; if one concept has a third wording, merge it into the first.
- The Chinese translation follows the English clause by clause: keep the clause order, the position of contrast and negation words, and quantifiers (a / one / another) as in the English. Do not reorder, merge, or split clauses even if the Chinese sounds slightly stiff. It must contain the same information as the English and add nothing.
- Do not add bullets, numbering, headings, markdown, blank lines, or explanations outside the Q:/A: lines.
- Do not use the delimiter ||| anywhere except between the target text and translation.

Generate the interview script now.
