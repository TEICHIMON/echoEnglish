# echoEnglish

## Audio prompt 同步规则（硬性要求）

凡是修改/新增任何关于 audio 生成 prompt 的说明，都必须同步更新 interview-notes 项目的 audio 命令。涉及范围包括但不限于：

- `interviewPrompt.md`（mock interview 脚本生成 prompt）
- webapp AI prompt helper 里的 prompt 文案（`webapp/static/index.html`、`webapp/static/app.js`）
- 影响脚本格式约定的 parser 行为（如 `Q:/A:` 前缀、`|||` 分隔、三语稿列序 EN→JA→ZH、注音假名剥离规则）

同步目标（两处哪个涉及改哪个）：

- `/Users/sudami/WebstormProjects/resume20260521/interview-notes/.claude/commands/audio.md`
- 该项目 `CLAUDE.md` 中的 `## AUDIO contract` 章节

改完后必须在回复中报告同步了哪些文件。
