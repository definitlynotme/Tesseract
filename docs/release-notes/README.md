# 应用内更新说明 / In-app release notes

`<版本>.txt`（例如 `0.1.2.txt`）是该版本发布时放进 release 正文、应用内「更新说明」显示的文本（见 `.github/workflows/release-windows.yml`）。纯文本：不要用 Markdown 标记（`**`、`##`），链接不写 `://`。

## 按语言分段

应用只显示当前界面语言的那一段。一行只写方括号里的语言代码，就开始这个语言的一段，直到下一个这样的行：

```text
[zh_CN]
修复：……

- ……

[en_US]
Fixed: ...

- ...
```

- 必须有 `[zh_CN]` 和 `[en_US]`，每种语言只出现一次，每段都要有内容（`tests/test_release_environment.py` 检查）。
- 没有单独一段的语言（日语、德语……）显示英文段；第一个标记之前的文字在每种语言里都显示。
- 没有任何标记的文件（0.1.1 及更早）整段显示。
- 0.1.2 之前安装的版本不认识分段，会把两段连同 `[zh_CN]` / `[en_US]` 这两行一起显示，所以标记行要保持这个可读的样子。

`ui/src/i18n/release_notes.js` 是分段规则的实现。

---

`<version>.txt` is the release's in-app notes. A line that is only a bracketed language code (`[zh_CN]`, `[en_US]`) starts that language's section; the app shows the section of its current UI language, English for a language without one, and the whole text when there is no marker. Both `[zh_CN]` and `[en_US]` are required.
