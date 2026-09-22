# RADIANT-LLM 无 Docker 部署记录

> 日期：2026-09-21 · 环境：无特权云容器（SSH + VS Code Remote 访问）
> 结论：本项目已在本机**完全跑通**，不依赖 Docker。启动命令见文末。

---

## 1. 遇到的问题

### 1.1 Docker Hub 直连超时
`docker compose pull` 报错 `Get "https://registry-1.docker.io/v2/": context deadline exceeded`。
原因：服务器在国内网络环境，Docker Hub 直连不通。

### 1.2 本机无法运行任何 Docker 容器（更根本的问题）
排查发现本机是**无特权云容器**，Docker 的三条必经之路全部被堵死：

| 检查项 | 结果 | 影响 |
|---|---|---|
| `CAP_NET_ADMIN` | 无 | dockerd 无法配置 iptables/网桥/端口映射 |
| `CAP_SYS_ADMIN` | 无 | 无法 mount，镜像层解压都会失败（overlayfs/vfs 均不行） |
| `kernel.unprivileged_userns_clone` | 0 | rootless Docker 也不可用 |

结论：**在这台机器上 Docker 路线彻底不可行**，只能绕过。

## 2. 解决方案：直接下载镜像并在宿主机原生运行

RADIANT-LLM 的 Docker 镜像本质是一个 **Python 3.12 + FastAPI/Dash** 应用
（入口 `radiant-llm-api`，Web UI 在 8080 端口）。因此：

1. **找可用镜像站**：`docker.m.daocloud.io` 将该镜像列入黑名单不可用；
   最终使用 **`docker.1ms.run`**（通过 Registry HTTP API v2 + Bearer token 直接拉 blob）。
2. **逐层下载并校验完整性**：镜像共 18 层 / 3.65 GB，每一层的 SHA256 都与
   Docker Hub 官方清单（manifest digest `sha256:800cd8dd...`）逐一比对通过，
   与 `docker pull` 所得**逐字节一致**。清单与层文件备份在 `image-layers-backup/`。
3. **手动解压层**（普通 `tar`，无需特权）：得到完整 rootfs。
4. **原生运行**：镜像内 `/usr/local` 是自包含的 Python 3.12.13（含 torch 2.7.0、
   langchain 0.3.13、chromadb、dash 2.18.2、transformers 4.45.2 等全部依赖），
   系统库（glibc、OpenSSL 等）直接使用宿主机的，经验证完全兼容。
5. **FUSE 盘的坑**：`/mnt`（fuse.fx）**不支持符号链接**，整体复制 rootfs 失败。
   改用 `cp -rL`（解引用，链接变实体文件），且只迁移必需的两部分：
   `usr/local`（运行时）+ `radiant-llm`（应用源码）。

## 3. 最终目录结构（全部在 /mnt/lina/RADIANT_LLM/ 下）

```
/mnt/lina/RADIANT_LLM/
├── start_radiant.sh        ← 启动/停止脚本
├── app/                    ← 应用源代码（改代码在这里，重启生效）
│   ├── radiant_llm.py          主逻辑
│   ├── api.py                  Web 服务入口（uvicorn + FastAPI）
│   ├── tools/  utils/          工具与辅助模块
│   └── system_prompt_radiant_llm.yml   系统提示词（可直接改）
├── runtime/                ← Python 3.12 + 全部依赖（来自镜像 /usr/local，勿动）
├── radiant_llm_skills/     ← 领域技能库（环境变量自动指向这里）
├── Docker_Executable/
│   ├── .env                ← API keys（OPENAI_API_KEY 已配置）
│   ├── data/               ← 放 PDF 等文件；UI 里 Working Directory 填此路径
│   ├── RADIANT_LLM_Logs/  RADIANT_LLM_Sessions/
├── image-layers-backup/    ← 官方镜像层 + manifest 备份（3.6G，稳定后可删）
└── ... 仓库原有的文档、skills、评测材料等
```

## 4. 日常使用

```bash
/mnt/lina/RADIANT_LLM/start_radiant.sh -d     # 后台启动（端口 8080）
/mnt/lina/RADIANT_LLM/start_radiant.sh stop   # 停止
tail -f /mnt/lina/RADIANT_LLM/radiant-llm.out.log   # 查看日志
```

- **访问界面**：VS Code「端口」面板转发 8080 → 浏览器打开 <http://localhost:8080>
  （或 `ssh -L 8080:localhost:8080 <服务器>`）。
- **启动较慢属正常**：从 FUSE 盘加载 torch 等库约需 1~2 分钟端口才通。
- **改源码**：编辑 `app/` 下文件，`stop` + `-d` 重启即生效，无需任何构建。
- **改 API keys**：编辑 `Docker_Executable/.env`，重启生效。

## 5. 尚待完善

- [ ] **HF_API_KEY 未配置**：聊天问答可用，但文档解析建知识库（RAG 核心功能）需要。
  到 <https://huggingface.co/settings/tokens> 免费申请，填入 `.env` 后重启。
- [ ] **tesseract 缺失**：解析扫描版（图片型）PDF 的 OCR 功能需要，
  可从镜像 `/usr/bin/tesseract` 及其依赖库补齐（普通文字版 PDF 不受影响）。
- [ ] **磁盘空间**：`/mnt` 分区仅剩约 3.4G。运行稳定后可删除
  `image-layers-backup/` 释放 3.6G（官方镜像在 Docker Hub 上随时可重新下载）。

## 6. 关键知识点备忘

- GitHub 仓库**不含主程序源码**（Proprietary 许可），应用本体只通过
  Docker 镜像 `zev94/radiant-llm` 分发——所以"下载镜像"是必需步骤，不是可选项。
- 镜像站测速参考（2026-09）：`docker.1ms.run` 可用但单连接约 200KB/s，
  需多线程分块下载（12 线程聚合约 3MB/s），大文件要防"低速僵尸连接"
  （curl 加 `--speed-limit 10240 --speed-time 60` 自动重连）。
- 后台任务要在 SSH 断连后存活：用 `setsid nohup ... &` 脱离会话。
