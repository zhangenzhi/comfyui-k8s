# ComfyUI on the Rancher / Kubernetes cluster

按 `ffformer/deploy` 同一套路（`c30636-default` 命名空间、PodSecurity `restricted`、
`runtimeClassName: nvidia`、PVC 持久化、NodePort + MetalLB + nginx TLS）部署 ComfyUI。

```
comfyui-k8s/
├── Dockerfile                 # pytorch cu124 底座 + ComfyUI + ComfyUI-Manager，非 root (UID 1000)
├── entrypoint.sh              # 初始化 PVC 目录、注入 Manager、启动 ComfyUI --base-directory
├── fetch-model.sh             # 镜像内小工具：fetch-model <subdir> <url>
├── docker-compose.yml         # 本机 GPU 冒烟测试
├── .github/workflows/         # push main -> 自动 build & push 到 Docker Hub
└── k8s/
    ├── 00-pvc.yaml            # comfyui-pvc (300Gi, RWO) -> /workspace/data
    ├── 10-deployment.yaml     # Deployment + ClusterIP svc + NodePort svc
    ├── 20-model-fetch-job.yaml# 批量下载模型到 PVC 的 Job
    └── 30-public-tls.yaml     # nginx TLS + Basic Auth + MetalLB LoadBalancer（可选）
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

> 底座 `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime`（CUDA 12.4，节点 12.8 驱动可跑）。
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

**公网 HTTPS（可选，`30-public-tls.yaml`）**

1. 向中心申请一个新的中间 IP（ffformer 的 172.31.229.16 不能复用），填进 `metallb.universe.tf/loadBalancerIPs`；
   `server_name` 填申请到的域名。
2. 创建证书和 Basic Auth 用户：
   ```bash
   openssl req -x509 -nodes -newkey rsa:2048 -days 365 -keyout tls.key -out tls.crt -subj "/CN=<hostname>"
   kubectl -n $NS create secret tls comfyui-tls --cert=tls.crt --key=tls.key
   htpasswd -Bc htpasswd <user>            # 没有 htpasswd 就: python3 -c "import bcrypt;print('<user>:'+bcrypt.hashpw(b'<pw>',bcrypt.gensalt()).decode())" > htpasswd
   kubectl -n $NS create secret generic comfyui-htpasswd --from-file=htpasswd
   ```
3. `kubectl apply -f k8s/30-public-tls.yaml`，等 `comfyui-public` 的 EXTERNAL-IP 不再是 `<pending>`。

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

## 6. 排错

| 现象 | 处理 |
|---|---|
| pod `Pending`，Events 里 `Insufficient nvidia.com/gpu` | GPU 被占；`kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu` 看空闲，或先缩掉别的 GPU 负载 |
| pod 起不来，Events 里 `violates PodSecurity "restricted"` | 别删 `securityContext` 那几行（runAsNonRoot / drop ALL / seccomp） |
| `CUDA error: no kernel image` / driver 版本不够 | 换底座为 cu121 或更低：`--build-arg BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime` |
| 一直 `startupProbe failed` | 自定义节点 import 太慢或崩；`kubectl logs` 看堆栈，必要时把出问题的目录从 `custom_nodes/` 移走 |
| PVC 一直 `Pending` | 没有默认 StorageClass；`kubectl get sc`，在 `00-pvc.yaml` 填 `storageClassName` |
| `kubectl` 报 `system:unauthenticated` | kubeconfig token 过期，回 Rancher 重新下载 |
| 页面能开但生图卡在 0%，Console 里 WebSocket 报错 | 走的代理没转发 Upgrade 头；用本仓库的 nginx 配置，或直接 port-forward |
