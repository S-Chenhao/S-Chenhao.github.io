# Chenhao Si Academic Homepage

中英双语个人学术主页，使用 Next.js 静态导出和 GitHub Pages 发布。

- 主页：https://s-chenhao.github.io/
- Google Scholar：https://scholar.google.com/citations?user=Vqf7dwEAAAAJ&hl=en

## Scholar 自动更新

`.github/workflows/deploy-pages.yml` 每天 **北京时间 08:23（UTC 00:23）** 尝试同步，GitHub 的定时任务可能延迟。向 `main` 推送修改或手动运行该 workflow 也会同步并发布。

流程：

1. 通过 SerpApi 的 Google Scholar Author API，从固定的公开 Scholar 账号 `Vqf7dwEAAAAJ` 获取完整论文列表、总引用数与 h-index。
2. 校验作者、指标、分页完整性和每篇论文的数据；全部有效后才更新 `public/scholar.json`。
3. 将 Scholar 中尚未列入主页的论文写入 `data/scholar-pending.json`，供确认。
4. 将数据提交到 `main`，用这份数据重新构建和发布主页。

主页展示最近一次**成功获取数据的时间**，时区为北京时间。首次同步尚未成功时，继续显示原来的 2026 年 9 月快照。同步失败时不会把旧值改成零，也不会更新成功时间；正常的网站修改仍可使用上次数据发布。失败会显示在 GitHub Actions 中，下一次定时运行会重试。超过 7 天没有成功同步，主页会提示仍在展示保留的数据。

GitHub 直接请求 Scholar 曾收到 HTTP 403，因此定时任务改为使用第三方 SerpApi 接口。需要在仓库 **Settings → Secrets and variables → Actions** 保存名为 `SERPAPI_API_KEY` 的 repository secret。密钥只传给同步步骤，不进入网页、JSON、日志或 Git 历史。没有读取旧模板的 `google-scholar-stats` 分支，旧模板的定时 workflow 已停用。

可使用 SerpApi 的 [Free 套餐](https://serpapi.com/pricing)。每页最多获取 100 篇，当前论文数量每天一次通常每月约 30–31 次查询；推送和手动运行会增加调用。程序不会购买额度、升级套餐或开启续费；额度不足时保留上次数据，下一次运行再尝试。接口说明见 [Google Scholar Author API](https://serpapi.com/google-scholar-author-api)。

### 立即刷新与排查

打开仓库 **Actions → Deploy academic homepage → Run workflow → main**。

- `Refresh Google Scholar`：抓取、校验并保存数据。
- `build` / `deploy`：构建并发布主页。
- 同步失败时查看抓取步骤日志；无需删除已有 JSON 或修改成功时间。
- `SERPAPI_API_KEY` 缺失：添加 repository secret 后重新运行；鉴权失败：检查密钥和 SerpApi 账号；额度不足：检查服务后台额度。不要把密钥写入工作流文件或公开日志。
- GitHub 会暂停长期无仓库活动的公开仓库定时任务。若 Actions 显示 workflow 被禁用，点击 **Enable workflow** 后手动运行一次。

Scholar 作者 ID 是公开信息，已固定在 workflow 参数中。需要保密的是 `SERPAPI_API_KEY`。

## 新论文确认

查看 `data/scholar-pending.json`。这份列表只记录最近一次成功同步时尚未列入主页的论文，不会自动覆盖中英文文案或论文链接。

确认后，将论文添加到 `data/publications.json`，填写：

- `scholarId`：待确认条目中的完整 ID（建议保留，以便论文标题变化后仍能匹配）。
- `title` / `titleZh`、`authors`、`venue`、`year`、`type`。
- `url`：论文链接；`code`：可选的代码链接。
- 其余字段参照已有条目。

提交到 `main` 后自动同步并发布，已确认论文将从待确认列表中移除。已有论文的引用数优先按 Scholar ID 匹配，没有 ID 时按规范化后的标题匹配；成功同步后找不到对应论文时显示缺失标记，避免将旧引用数误标为最新数据。

## 常用修改位置

- 个人介绍、中英文文案、联系方式：`app/page.tsx`
- 已确认论文及其译名、展示顺序、链接：`data/publications.json`
- 最近成功同步的 Scholar 数据：`public/scholar.json`（由程序维护）
- 新论文待确认列表：`data/scholar-pending.json`（由程序维护）
- 页面样式：`app/globals.css`
- 页面和分享元数据：`app/layout.tsx`
- 个人照片：`public/profile.jpg`
- 分享预览图：`public/og.png`
- API 同步程序及离线测试：`scripts/sync_scholar_api.py`、`scripts/test_sync_scholar_api.py`
- 共用数据保存逻辑及旧版 HTML 解析：`scripts/sync_scholar.py`、`scripts/test_sync_scholar.py`（定时任务不再直接请求 Scholar HTML）
- 同步与发布：`.github/workflows/deploy-pages.yml`

## 本地开发与验证

需要 Node.js 22.13 或更高版本、pnpm 11.19 和 Python 3.12。

```bash
pnpm install --frozen-lockfile
python3 -m unittest discover -s scripts -p 'test_*.py' -v
pnpm run build:pages
```

静态文件输出到 `out/`。运行 `pnpm run dev` 可启动本地预览。

手动同步（先在本地环境中配置 `SERPAPI_API_KEY`，不要把密钥提交到 Git）：

```bash
python3 scripts/sync_scholar_api.py --scholar-id Vqf7dwEAAAAJ --curated data/publications.json
```

仓库 **Settings → Pages → Source** 应保持 **GitHub Actions**。
