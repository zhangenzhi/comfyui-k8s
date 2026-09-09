# ComfyUI on the Rancher / Kubernetes cluster

按 `ffformer/deploy` 同一套路（`c30636-default` 命名空间、PodSecurity `restricted`、
`runtimeClassName: nvidia`、PVC 持久化、NodePort + MetalLB + nginx TLS）部署 ComfyUI。

```
comfyui-k8s/
├── Dockerfile                 # pytorch cu124 底座 + ComfyUI + ComfyUI-Manager，非 root (UID 1000)
├── entrypoint.sh              # 初始化 PVC 目录、注入 Manager、启动 ComfyUI --base-directory
├── fetch-model.sh             # 镜像内小工具：fetch-model <subdir> <url>
├── custom_nodes/comfyui-minimax-h3/  # H3 节点（见 §6）
├── docker-compose.yml         # 本机 GPU 冒烟测试
├── .github/workflows/         # push main -> 自动 build & push 到 Docker Hub
└── k8s/
    ├── 00-pvc.yaml            # comfyui-pvc (300Gi, RWO) -> /workspace/data
    ├── 10-deployment.yaml     # Deployment + ClusterIP svc + NodePort svc
    ├── 20-model-fetch-job.yaml# 批量下载模型到 PVC 的 Job
    └── 30-public-tls.yaml     # 公网入口说明：复用 ffformer 的 nginx，按 /test/comfyui/ 路径分发
```

## 0. 和 ffformer 的差异

| 项目 | ffformer | ComfyUI |
|---|---|---|
| 端口 | 8000 (FastAPI) | 8188 |
| 健康检查 | `/health` | `/system_stats` |
| 持久目录 | `/workspace/data`（只放权重） | `/workspace/data`（models / custom_nodes / input / output / user / .pyuser 全在里面） |
| 代码更新 | 启动时 `git pull` | 默认关闭（`COMFYUI_AUTO_UPDATE=0`），可开 |
| 认证 | 应用自带 | **ComfyUI 没有登录**，公网出口在 nginx 加 Basic Auth |
| WebSocket | 有 | 有（`/ws`，nginx 已配 Upgrade） |

## 1. 构建镜像

本超算登录节点没有 docker，和 ffformer 一样走 GitHub Actions：

仓库：https://github.com/zhangenzhi/comfyui-k8s 。push 到 `main`（改了 Dockerfile / entrypoint / fetch-model）
会自动构建并推到 **GHCR**：`ghcr.io/zhangenzhi/comfyui:latest` 和 `:<sha>`，用的是 Actions 自带的
`GITHUB_TOKEN`，不需要配 Docker Hub 密钥。也可在 Actions 页 `workflow_dispatch` 手动触发并指定
`comfyui_ref`（建议钉一个 tag，如 `v0.3.xx`）。

```bash
gh workflow run docker-publish.yml -R zhangenzhi/comfyui-k8s -f comfyui_ref=master
gh run watch -R zhangenzhi/comfyui-k8s
```

镜像包需要是 **public** 集群才能免密拉取（首次构建后到 GitHub → Packages → comfyui → Package settings 确认）。

本机有 GPU 的话可以先 `docker compose up --build` 打开 http://localhost:8188 冒烟。

> 底座 `pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime`（ComfyUI 当前 master 需要 torch ≥ 2.8；节点驱动支持 CUDA 12.8）。
> 想换 torch/CUDA 版本改 `--build-arg BASE_IMAGE=...` 即可。

## 2. 部署

```bash
export KUBECONFIG=~/.kube/config     # token 过期(system:unauthenticated)就回 Rancher 重新下载
NS=c30636-default

kubectl apply -f k8s/00-pvc.yaml
kubectl apply -f k8s/10-deployment.yaml

kubectl -n $NS get pvc comfyui-pvc                 # Bound
kubectl -n $NS get pods -l app=comfyui -w          # Running / READY 1/1
kubectl -n $NS logs -f deploy/comfyui              # 看到 "To see the GUI go to: http://0.0.0.0:8188"
```

也可以在 Rancher 网页 **Import YAML** 把 `00` + `10` 一起贴进去（多文件用 `---` 分隔）。

## 3. 访问

**Port-forward（最快，无需公网）**
```bash
kubectl -n $NS port-forward svc/comfyui-svc 8188:8188
# 浏览器打开 http://localhost:8188
```

**NodePort**
```bash
kubectl -n $NS get svc comfyui-nodeport      # 看 PORT(S) 里 8188:3xxxx，用 <任一节点IP>:3xxxx
```

**公网 HTTPS（已配置，走 ffformer 的入口按路径分发）**

```
https://j-peaks-forestformer3d.hucc.hokudai.ac.jp/test/comfyui/
```

不需要新 IP / 端口 / 域名：`ffformer-tls` 的 nginx 里加了 `location /test/comfyui/`，去掉前缀后转给
`comfyui-svc:8188`，并做 Basic Auth（用户名密码在登录节点 `~/.comfyui-basic-auth`）。
配置来源是 `ffformer/deploy/k8s-https.yaml`，改完要 `kubectl apply` 再 `rollout restart deploy/ffformer-tls`。
加用户 / 改密码见 `k8s/30-public-tls.yaml` 里的说明。公网 IP 只放行校园网段，超算登录节点连不上，属正常。

## 4. 放模型

模型都在 PVC 的 `/workspace/data/models/<类别>/` 下，三种办法：

```bash
# a) 在运行中的 pod 里直接下（最省事）
kubectl -n $NS exec deploy/comfyui -- fetch-model checkpoints \
  https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors

# b) 批量：编辑 k8s/20-model-fetch-job.yaml 里的列表，然后
kubectl -n $NS scale deploy comfyui --replicas=0     # PVC 是 RWO，先停 ComfyUI
kubectl apply -f k8s/20-model-fetch-job.yaml && kubectl -n $NS logs -f job/comfyui-model-fetch
kubectl delete -f k8s/20-model-fetch-job.yaml && kubectl -n $NS scale deploy comfyui --replicas=1

# c) 从超算本地拷（大文件慢，但对 lustre 上已有的权重方便）
kubectl -n $NS cp /lustre1/work/c30636/models/xxx.safetensors \
  $(kubectl -n $NS get pod -l app=comfyui -o name | cut -d/ -f2):/workspace/data/models/checkpoints/
```

Gated 的 HF 模型（FLUX.1-dev 等）先建 token：
```bash
kubectl -n $NS create secret generic comfyui-hf-token --from-literal=token=hf_xxx
```
放完模型后在 UI 里按 **R**（Refresh）或重启 pod 即可看到。

## 5. 自定义节点 / 更新

- **ComfyUI-Manager** 首次启动时自动注入到 `/workspace/data/custom_nodes/`，之后通过 UI 的 Manager 装节点、装模型。
  节点 pip 依赖走 `PIP_USER=1`，装到 PVC 的 `.pyuser/`，重启不丢。
  监听 0.0.0.0 时 Manager 默认 `security_level=normal` 会禁用 "从 git URL 安装"，需要的话改
  `/workspace/data/user/default/ComfyUI-Manager/config.ini` → `security_level = weak`（仅内网/有 Basic Auth 时）。
- **更新 ComfyUI 本体**：重新 build 镜像（推荐，可回滚），或把 Deployment 的 `COMFYUI_AUTO_UPDATE` 设为 `"1"`
  然后 `kubectl -n $NS rollout restart deploy/comfyui`。
- **改启动参数**：改 `COMFYUI_ARGS`（如 `--highvram`、`--fp8_e4m3fn-unet`、`--fast`）后 rollout restart。

## 6. MiniMax-H3：ComfyUI 接任务，超算生成

`custom_nodes/comfyui-minimax-h3/`（随镜像打包，每次启动同步到 PVC）提供三个节点，分类 **MiniMax-H3 (HPC)**：

| 节点 | 作用 |
|---|---|
| **MiniMax-H3 Generate (HPC)** | 把 prompt（+ 可选首/末帧、参考图）提交给超算上的 SGLang `/v1/videos`，轮询进度，把 mp4 存到 `output/h3/`，输出 VIDEO |
| **H3 Prompt Rewrite (Ollama)** | 用集群里的 `ollama`（qwen2.5:7b-instruct）把任意语言的一句话想法改写成 H3 官方三段式提示词（替代未开源的 H3-Context-IR） |
| **H3 Server Status** | 探测服务健康和当前任务数 |

链路：`浏览器 → ComfyUI pod → HTTP 直连 sgpu0xx:30010（SGLang，4×H100）→ mp4 拉回 pod 的 output/`。
pod 能直接访问计算节点端口，所以生成主链路**不经过 SSH**；图片以 `data:image/png;base64` 内嵌在请求里。

**服务端**：`/lustre1/work/c30636/test/minimax-h3`，`qsub scripts/serve_h3.pbs` 起服务（12h walltime），
端点写在 `logs/server_endpoint.txt`（主机名）。pod 解析不了 HPC 主机名，Deployment 里的 `H3_ENDPOINT`
要填 **IP:端口**（`getent hosts sgpu016`）。作业换了节点就改 `H3_ENDPOINT` 后 `rollout restart`，
或者在节点的 `endpoint` 输入框里临时填。

**可选 SSH 自动发现**：给 pod 一把受限的 key（`from="172.31.232.*",no-pty`，同 ffformer），
做成 Secret `comfyui-hpc-ssh`（key 名 `key`），Deployment 已挂到 `/secrets/hpc/key`；
`H3_ENDPOINT` 留空时节点会通过 SFTP 读 `server_endpoint.txt` 并在登录节点上解析 IP。

```bash
ssh-keygen -t ed25519 -N '' -C comfyui-k8s-pod -f ~/.ssh/comfyui_k8s_ed25519
echo "from=\"172.31.232.*\",no-pty,no-agent-forwarding,no-X11-forwarding $(cat ~/.ssh/comfyui_k8s_ed25519.pub)" >> ~/.ssh/authorized_keys
kubectl -n c30636-default create secret generic comfyui-hpc-ssh --from-file=key=$HOME/.ssh/comfyui_k8s_ed25519
kubectl -n c30636-default rollout restart deploy/comfyui
```

参数默认值来自 `scripts/t2va_request.sh`：768p short edge、50 步、flow_shift 12、audio_flow_shift 3，时长 4–15 s。
一集短剧要拆成多段 ≤15 s 生成再拼接。

## 7. 短剧管线：剧本 → 分镜 JSON → 逐段 H3 → 拼接

System Prompt v2（机器可读分镜版）在 `custom_nodes/comfyui-minimax-h3/drama_system_prompt.md`，
原稿也存于 `minimax-h3/prompts/templates/douyin_drama_system_prompt_v2.md`。人物圣经在 `bible.py`。

| 节点（分类 MiniMax-H3 (HPC)/drama） | 作用 |
|---|---|
| **H3 Episode Planner** | 用 v2 提示词写一集：输出中文剧本 + 分镜计划 JSON（5–7 段 ≤15 s，每镜头带秒数/镜头语言/台词/说话人）。`backend=local` 用 pod 自己的 H100 跑 `Qwen2.5-14B-Instruct`（推荐），`ollama` 走集群 CPU 服务（慢） |
| **H3 Clip Prompt Builder** | 确定性地把第 N 段转成 H3 三段式英文提示词（自动插入固定外形/声线、S1/S2、画外音闭嘴规则、时间戳），并输出该段的中文 SRT |
| **H3 Video Concat + Subtitles** | ffmpeg 归一化、烧中文字幕（Noto Sans CJK）、硬切或交叉淡化拼接成整集 |

推荐工作流（一集 5 段）：

```
Episode Planner ─ shot_plan_json ─┬─ Clip Prompt Builder(1) ─ h3_prompt/duration ─ MiniMax-H3 Generate(t2va) ─ video_path ─┐
                                  ├─ Clip Prompt Builder(2, fl2va) ← first_frame = 上一段末帧(GetVideoComponents→ImageFromBatch) ─ Generate(fl2va) ┤
                                  ├─ …                                                                                                        ├─ Video Concat + Subtitles
                                  └─ Clip Prompt Builder(5) ……                                                                                ┘
```

- **人物一致性**：固定外形句 + 固定声线句每段重复；第 2 段起用 fl2va，把上一段最后一帧作为首帧（场景、光线、站位自然延续）；
  需要更强的人脸一致性时，先在 pod 的 H100 上用 SDXL + IPAdapter/PuLID 出两位主角的定妆参考图，再走 `ref2va`（需先下载 `Ref2VA/` 权重并以 `VARIANT=ref2va` 起服务）。
- **动作可控**：一镜一事；复杂肢体动作拆镜头；关键姿势用 ControlNet(OpenPose) 在 SDXL 上出关键帧，再 fl2va 强制首末帧。
- **画质**：H3 原生 768p。后期在 pod 上：`FrameInterpolate`(RIFE) 24→48 fps、`ImageUpscaleWithModel`(4x-UltraSharp) 逐帧放大到 1080p/2K、
  再 `CreateVideo`+`SaveVideo`；或直接在请求里开 SGLang 的 `enable_upscaling` / `enable_frame_interpolation`（服务端算，占 H100 时间）。
- 本地 LLM 权重：`kubectl exec deploy/comfyui -- fetch-llm Qwen/Qwen2.5-14B-Instruct`（约 30 GB，进 PVC `/workspace/data/llm/`）。
  字幕字体：`/workspace/data/fonts/NotoSansCJK-Regular.ttc`（镜像内也装了 fonts-noto-cjk）。

## 8. 排错

| 现象 | 处理 |
|---|---|
| pod `Pending`，Events 里 `Insufficient nvidia.com/gpu` | GPU 被占；`kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu` 看空闲，或先缩掉别的 GPU 负载 |
| pod 起不来，Events 里 `violates PodSecurity "restricted"` | 别删 `securityContext` 那几行（runAsNonRoot / drop ALL / seccomp） |
| `CUDA error: no kernel image` / driver 版本不够 | 换底座为 cu126：`--build-arg BASE_IMAGE=pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime`（再低的 torch 跑不了当前 ComfyUI） |
| 一直 `startupProbe failed` | 自定义节点 import 太慢或崩；`kubectl logs` 看堆栈，必要时把出问题的目录从 `custom_nodes/` 移走 |
| PVC 一直 `Pending` | 没有默认 StorageClass；`kubectl get sc`，在 `00-pvc.yaml` 填 `storageClassName` |
| `kubectl` 报 `system:unauthenticated` | kubeconfig token 过期，回 Rancher 重新下载 |
| 页面能开但生图卡在 0%，Console 里 WebSocket 报错 | 走的代理没转发 Upgrade 头；用本仓库的 nginx 配置，或直接 port-forward |
