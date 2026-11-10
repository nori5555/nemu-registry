# Aidoku 合并目录（Nemu / Aidoku 可用）

由 6 个 Aidoku 源目录去重合并而成，共 **196** 个可安装 `.aix` 源；图标已本地化。

## 添加方式

发布到 HTTPS 静态托管后：

- **Nemu**：设置 → 源 → 添加源目录，填目录地址（Nemu 会自动补 `/index.min.json`）
- **Aidoku**：设置 → 源列表 → 添加，填 `.../index.min.json`
  深链：`aidoku://addSourceList?url=<index.min.json 的 URL>`

必须 HTTPS，Nemu 拒绝 localhost / 私网 / 带账号密码的地址。

## 内容

| 语言 | 源数 |
|---|---|
| en | 74 |
| zh | 48 |
| ja | 24 |
| fr | 22 |
| vi / ru | 17 / 17 |
| es / id | 16 / 15 |
| th / it / de | 10 / 9 / 9 |
| multi | 8 |

来源目录：

- aidoku-community（150）
- suiyuran/aidoku-zh-sources（25）
- Aidoku-Community/legacy-sources（34）
- AlanLang/aidoku-sources（2）
- Moomooo95/aidoku-french-sources（10）
- beer-psi/aidoku-ln-sources（1）

去重后 196；其中 127 个站点与 Keiyoushi 的扩展对应。

## 限制

- 只是 Aidoku 生态已有实现的合集，**没有**把 Mihon 的 Kotlin 扩展转成 WASM。
  Keiyoushi 的 2547 个站点里只有约 127 个存在 `.aix` 实现，其余需要逐站重写。
- 各源的可用性取决于站点自身反爬和 Nemu 的浏览器运行时，可能出现 Cloudflare 拦截。
- 图标路径为相对路径，移动/重命名 `icons/` 会导致图标 404。

## 重新生成

```bash
python3 /var/minis/skills/mihon2aidoku/scripts/mihon2aidoku.py convert \
  --input https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.json \
  --aidoku "<目录A>,<目录B>,..." --scope all --target nemu \
  --list-name "Aidoku 合并目录" --out ./registry
```

`meta/` 目录只用于分析和留档，不需要发布。
