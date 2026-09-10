# HPC 侧：MiniMax-H3 服务 + SeedVR2 超分（PBS）

这一半跑在超算上：MiniMax-H3 视频+音频生成服务（SGLang，4×H100，队列 c30636g）、SeedVR2-7B 2K 超分（队列 sg，单卡），
以及可选的 Qwen2.5 剧本 LLM 备用服务。研究云上的 ComfyUI 直接 HTTP 调 H3 服务，超分通过 SSH 投 PBS 作业（见顶层 README）。

## 从零部署

脚本和提示词在本仓库 `hpc/`；权重、输出、日志放在一个**工作目录**（不进 git），所有脚本通过环境变量 `H3_ROOT` 找它
（默认 `/lustre1/work/c30636/test/minimax-h3`）。

```bash
export H3_ROOT=/lustre1/work/<group>/<you>/minimax-h3        # 工作目录：models/ outputs/ logs/ tools/
REPO=/path/to/comfyui-k8s
mkdir -p $H3_ROOT && ln -sfn $REPO/hpc/scripts $H3_ROOT/scripts && ln -sfn $REPO/hpc/prompts $H3_ROOT/prompts
cd $H3_ROOT
bash scripts/setup_env.sh              # conda 环境 minimax-h3（python3.12 + ffmpeg + uv + sglang[diffusion]）
bash scripts/download_models.sh        # H3 FL2VA ~139 GB、SeedVR2 7B sharp ~17 GB、SeedVR2 CLI @4490bd1
bash scripts/setup_seedvr2_env.sh      # conda 环境 seedvr2（超分）
# 改成自己的账号/队列：*.pbs 里的 #PBS -q / -W group_list / -o（PBS 指令里不能用环境变量）
qsub -l walltime=48:00:00 scripts/serve_h3.pbs   # 起服务，日志出现端口监听后：
cat logs/server_endpoint.txt                     # 例如 sgpu016:30010；ComfyUI 通过 SSH 读这个文件自动发现
scripts/t2va_request.sh "一只猫在海边散步，海浪声" 5 16:9 42   # 冒烟测试
```

注意：PBS 作业有 walltime，到期服务会被杀，正在渲染的 ComfyUI 工作流会中断；日常用 48 h（队列上限 336 h），用完 `qdel`。
同一节点被别的 4 卡作业占着时，`h3_serve` 会一直排队（`Not Running: Insufficient amount of resource: Qlist`）。


MiniMax-H3 = 33B 单流 Omni-Transformer（视频+立体声音频联合生成）+ Qwen3-VL-32B 文本编码器 + 视频/音频 VAE。
开源部分为 H3-Base（768p）。H3-Context-IR（提示词改写）和 H3-Regenerate-2K 未开源，只能走 MiniMax 官方 API。

## 目录
- `models/MiniMax-H3/`  权重根目录（必须指向根目录，sglang 自己按 `--model-variant` 选 `FL2VA/` 或 `Ref2VA/`）
  - `FL2VA/`  已下载（~139 GB）：t2va（文生视频）+ fl2va（首/尾帧）
  - `Ref2VA/` 未下载（再 ~144 GB）：ref2va（图/视频/音频参考）。需要时：
    `hf download MiniMaxAI/MiniMax-H3 Ref2VA/ --local-dir models/MiniMax-H3`
- `scripts/setup_env.sh`   建 conda 环境 `minimax-h3`（python3.12 + ffmpeg + uv + sglang[diffusion]）
- `scripts/serve_h3.pbs`   PBS 作业：在 c30636g 上起 sglang 服务（4 卡，TP2 + Ulysses2，端口 30010）
- `scripts/t2va_request.sh` 客户端：向服务提交 t2va 请求并下载 mp4 到 `outputs/`
- `logs/`  PBS 日志（`<jobid>.sjms.OU`）、`server_endpoint.txt`（服务运行时写入 host:port）

## 用法
```bash
qsub scripts/serve_h3.pbs                 # 起服务；等日志出现 "Application startup complete" / 端口监听
cat logs/server_endpoint.txt              # 例如 sgpu016:30010
scripts/t2va_request.sh "一只猫在海边散步，海浪声" 5 16:9 42
VARIANT=ref2va qsub scripts/serve_h3.pbs  # Ref2VA 服务（需先下载 Ref2VA/）
```
请求格式（OpenAI 兼容 `/v1/videos` 异步接口）见 `scripts/t2va_request.sh`；
`target.duration_seconds` 4–15 s，`short_edge` 768，`num_inference_steps` 50，`flow_shift` 12，`audio_flow_shift` 3。
输出：H.264 24 fps + AAC 32 kHz 立体声 mp4。

## 拓扑选择（SGLang cookbook 在 4×H100 80GB 上实测）
| 拓扑 | 单次流水线延迟 | 峰值显存/卡 |
|---|---:|---:|
| TP2 + Ulysses2（默认） | 13.25 s | 66 GB |
| FSDP + Ulysses4 | 13.36 s | 57 GB |
| TP4 + Ulysses1 | 13.86 s | 50 GB |
纯 Ulysses4 在 80 GB 卡上放不下全部权重（README 里的示例命令是给更大显存卡的）。

## 参考
- 模型卡: https://huggingface.co/MiniMaxAI/MiniMax-H3
- SGLang cookbook: https://docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3
- 提示词写法: `models/MiniMax-H3/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md`
- 许可: `models/MiniMax-H3/LICENSE`（MiniMax-H3 Community License）

## 测试素材：抖音短剧模板
- `prompts/templates/douyin_drama_system_prompt.md` 用户提供的短剧编剧 system prompt（给 LLM 出分镜用，原文保存）。
- `prompts/ep01_hook_t2va.txt` 把示范第一集"黄金前3秒钩子 + 镜头1–3"改写成 H3 官方格式（integrated_multimodal_description / overall_soundscape / non_diegetic_music，中文台词用 `<d>[Chinese] …</d>`），15 s、9:16 竖屏。
  H3 单次最多 15 s，所以一集 60–90 s 要按镜头组切成多段生成再剪。跑法：
  `PROMPT_FILE=prompts/ep01_hook_t2va.txt TAG=ep01_hook scripts/t2va_request.sh "" 15 9:16 7`
- 官方提示词规范：`models/MiniMax-H3/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md`。官方流程里这一步由 H3-Context-IR（未开源）完成，本地要么手写，要么让 LLM 按该规范改写。

## 实测（2026-09-09, 作业 637050, TP2+Ulysses2）
- 模型加载约 4 min（text_encoder 13.2 GB + DiT 30.9 GB + VAE 5.8 GB ≈ 50 GB/卡，加载后剩 26 GB/卡），预热请求 1 min 44 s。
- 5 s / 16:9 / 1344×768 / 50 步：87 s 出片（含编码器、VAE 解码与封装）。
- 服务对登录节点可达：`curl http://sgpu016:30010/health`。端口 30011 是内部 ZMQ，不要用。
- 已知坑：PBS 把 `CUDA_VISIBLE_DEVICES` 设为 GPU UUID，sglang 要整数索引，`serve_h3.pbs` 里已做映射；`~/.local` 里的旧 huggingface_hub 会遮蔽环境，需 `PYTHONNOUSERSITE=1`。

## 速度分析（2026-09-09）
每步耗时基本恒定，总时长 ≈ 步数 × 单步；解码/编码只占几秒。单步时间随 token 数（时长×分辨率）超线性增长，长片段是注意力主导。
| 请求 | 单步 | 去噪 | 端到端 |
|---|---:|---:|---:|
| 5 s 16:9 1344×768, 50 步（≈33k token） | 1.63 s | 80 s | 87 s |
| 5 s, 30 步 | 1.63 s | 47 s | 54 s |
| 15 s 9:16 768×1344, 50 步（≈93k token） | 8.94 s | 432 s | 448 s |
对照 SGLang 官方 4×H200 同负载实测：去噪 75–81 s、端到端 78–85 s。本部署已达该硬件的预期速度。
按 FLOP 估算（50 层、hidden 5376、56×128 头、FFN 14336）：5 s 约 2.6 PFLOP/步，15 s 约 15 PFLOP/步（其中注意力占 ~80%），
折合每卡约 400–430 TFLOPS，即 bf16 稠密峰值的 40–45%。真实 `nvidia-smi` 采样从下次启动开始记录在 `logs/gpu_dmon_<jobid>.log`。

可用的提速手段（按代价从小到大）：
1. 减少步数：`STEPS=30` 立即省 40%，画面构图一致，但道具细节丢失（50 步能看到铜管乐器，30 步基本没有）、画面偏暗偏糊，对提示词遵循度下降（对比图 `outputs/frames/steps50_vs_30.png`）。
2. Turbo LoRA（蒸馏少步）：larryvrh 8 步版（`num_inference_steps: 9`，约 6×）或 lightx2v 4 步版（`num_inference_steps: 5`，约 12×），需重启服务加 `--lora-path`，画质有损。
3. SageAttention（`--attention-backend sage_attn`，Hopper 需源码装 SM90 修复版）：注意力量化，15 s 长片段收益最大，近似算法。
4. FastH3 4 步 + VSA 稀疏注意力：只支持 t2va，另下约 60 GB 权重，B300 上 15 s 片段 13 s 出片；Hopper 上 Triton 内核支持 SM90，未在 H100 验证。
不可用/无效：`quality: "high"`（Cache-DiT）被硬编码限制为 4×H200 Ulysses4 部署；FP8 只在 B200/B300 验证且只加速线性层；torch.compile 与 CUDA graph 官方实测无收益。

## 吞吐（单服务串行，24 h 能产出多少成片）
| 单段时长 | 端到端 | 24 h 段数 | 24 h 成片总长 |
|---|---:|---:|---:|
| 5 s（16:9, 50 步） | 87 s | ~990 | ~86 min |
| 10 s | 232 s | ~370 | ~63 min |
| 15 s | 448 s | ~190 | ~48 min |
短片段单位产出更高（注意力开销随时长平方增长）。服务一次只处理一个请求，多请求排队不会更快。
估算：30 步约 ×1.6；Turbo 8 步 LoRA 约 5 s 段 6 h、15 s 段 4 h 成片/天；4 步版再翻倍（画质有损，未在本机验证）。

## 分辨率与调节手段一览（2026-09-09 实测 + 文档）
分辨率：`target.short_edge` 任意整数，画布对齐到 32 的倍数，总像素上限 768×1344 ≈ 1.03 MP（`resolved_plan.py`），超过就被截回；
所以本地最高就是 768p（16:9 → 1344×768，9:16 → 768×1344，1:1 → 1024×1024 左右）。2K 只能走 MiniMax 闭源的 Regenerate-2K API。
宽高比预设 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16，也接受 1:4 到 4:1 之间的任意比例。时长 4–15 s，24 fps 固定。
| 5 s 片段 | 画布 | 单步 | 端到端 | 备注 |
|---|---|---:|---:|---|
| short_edge 480 | 864×480 | 0.51 s | 31 s | 非官方验证尺寸，画面仍连贯（`outputs/frames/768p_vs_480p.png`） |
| short_edge 768（推荐） | 1344×768 | 1.63 s | 87 s | 官方配方 |
| short_edge 1080 | 被截为 1344×768 | 1.63 s | 84 s | 像素预算上限 |

## 社区衍生版本一览（2026-09-09，HF 下载量为参考）
**少步蒸馏（提速最大，有损）**
- lightx2v/Minimax-h3-Turbo（131 万下载）：4 步/8 步 LoRA，FL2V 和 Ref2V 各有，2 GB。4 步版理论 ≈12×。
- larryvrh/MiniMax-H3-Turbo-Lora（39 万）：8 步版，SGLang 推荐的画质/速度平衡点（`num_inference_steps: 9`）。
- alibaba-pai/MiniMax-H3-Acc-LoRAs（6.8 万）：阿里 PAI 的 8 步加速 LoRA，FL2VA/Ref2VA 各 1.4 GB。
- FastVideo FastH3 4-step VSA（11 万）：4 步 DMD2 蒸馏 + 稀疏注意力，整模 ~70 GB，只支持 t2va。B300 4 卡 15 s 片段 13 s 出片，比实时还快。
**省显存（不提速或略降速）**
- AdaLN 剪枝版（multimodalart、Comfy-Org、DeepBeepMeep）：DiT 66 GB → 40 GB，近似。
- SGLang `--minimax-h3-adaln-online true`：从 GPU 卸掉 24 GB AdaLN 权重，位精确，H100 上可考虑（未测）。
- INT8 ConvRot（Comfy-Org、Abiray）：DiT 34 GB，剪枝版 21 GB；文本编码器 27 GB。4090 上 1.34×（PSNR 24.8 dB）。
- W4A8 / INT4 / NVFP4（Merserk、Winnougan、Abiray、Kijai）：DiT 11–18 GB。NVFP4 需要 Blackwell。
- GGUF（unsloth 70 万、leejet）：Q2–Q8，剪枝版 Q4 只要 11 GB，纯容量路径，不支持 FSDP/LoRA。
- 小编码器 ClipProj（NicoLab28）：用 Qwen3-VL 4B/8B 替代 32B 编码器，近似条件。
**近似注意力（提速，有损；4090 实测叠加 int8）**
- sage_attn 2.32×、sol_attn 1.81×、Sage→Sol 混合 2.48×（PSNR 23–24.4 dB）。Hopper 上 Sage 要装 SM90 修复版。
**融合/微调**：Singularity（FL/Ref 融合 hybrid，一套权重三种任务）、MATLOWAI fused turbo int8、Kijai 实验仓（ControlNet、VSA int8）。
**引擎**：SGLang（本部署）、vLLM-Omni（2×24 GB 卡 DLO 分层卸载路径）、ComfyUI（Comfy-Org 权重，消费卡主流）、diffusers。

## 2K 超分：开源可选方案（2026-09-09 调研）
官方 H3-Regenerate-2K（把 768p 结果+原始上下文喂回 H3 在 2K 重生成）未开源，sglang 也没有对应模式，且本地有 768×1344 像素上限（`resolved_plan.py: MINIMAX_H3_MAX_PIXELS`）。
1. **通用视频超分后处理**（推荐先用）：
   - SeedVR2（字节，Apache-2.0，单步扩散修复，3B/7B，Comfy-Org 有 fp16/fp8/int8 版，7B 另有 sharp 变体）。ComfyUI 原生支持；原仓库支持多卡 sp_size。
   - FlashVSR v1.1（Apache-2.0，Wan 架构流式 4× 超分，稀疏注意力在 A100 加速理想、Hopper 受限）。固定 4×，768p→3072p 再缩到 2K。
   - 逐帧 Real-ESRGAN 类：快但闪烁，只适合草稿。
   缺点：都看不到提示词和参考图，小字/细节靠猜，这正是官方 regen 方案的优势。
2. **H3 潜空间上采样 + 重采样**（社区，ComfyUI）：LBH-123-AI/Minimax_h3_latent_Upscaler，0.7 GB 3D 卷积，直接在 24 通道潜变量上 1–4× 放大，再用 H3 以部分噪声强度精修。思路最接近官方 regen，但要在 ComfyUI 里跑，精修步在 2K 的 token 数≈4×（5 s 2K ≈ 13 万 token，约等于 15 s 768p 的计算量）。
3. **直接改像素上限让 H3 原生出 2K**：一行常量，需重启服务。基座是否在 2K 训练过未知，可能出现重复纹理；可做一次 4 s 实验验证。

## 2K 后处理：SeedVR2-7B sharp（2026-09-09 已部署）
- 环境 `seedvr2`（conda，torch 2.13 cu130，无 flash-attn，SDPA 后端）；代码 `tools/ComfyUI-SeedVR2_VideoUpscaler`（numz 的独立 CLI）；权重 `models/SeedVR2/`（7B sharp fp16 16.5 GB + VAE 0.5 GB，Apache-2.0）。
- 作业 `scripts/upscale_seedvr2.pbs`，走 `sg` 队列单卡：`qsub -v IN=/abs/clip.mp4 scripts/upscale_seedvr2.pbs`，可加 `RES=`（短边，默认 1440）、`BATCH=`（4n+1，默认 33）。输出 `outputs/upscaled/<name>_seedvr2_1440p.mp4`，音轨从原片复用。
- 一键：`scripts/h3_2k.sh "prompt" 5 16:9 42`（先 H3 再自动排超分）。
- 实测（5 s 1344×768 → 2520×1440，124 帧，单卡 H100）：总 173 s = VAE 编码 36 s + DiT 38 s（含 16 GB 权重加载）+ 分块 VAE 解码 75 s + 后处理 10 s；峰值显存 36 GB，DiT 阶段 SM 72–100%。
  解码占一半时间，80 GB 卡可去掉 `--vae_decode_tiled` 提速；`--compile_dit/--compile_vae` 官方称再省 20–40%。
- 15 s 768×1344 → 1440×2520（362 帧）：429 s，0.84 fps。
- 对比图 `outputs/frames/seedvr2_crop_compare.png`、`seedvr2_ep01_crop_compare.png`（左 bicubic，右 SeedVR2）：布纹、皮肤、毛发细节明显恢复。
- 提速变体实测（同一 5 s 片段，2026-09-10）：
  | 配置 | 总耗时 | 编码/DiT/解码 | 峰值显存 |
  |---|---:|---|---:|
  | 分块 VAE（默认，`TILED=1`） | 173 s | 36 / 38 / 75 s | 36 GB |
  | 不分块（`TILED=0`） | 155 s | 36 / 37 / 72 s | 74 GB |
  | 不分块 + torch.compile（`COMPILE=1`） | 568 s | 编译各约 3 min，解码阶段多次 OOM 回退 | 81 GB |
  结论：不分块只快 10%，显存翻倍到 74 GB，收益小；compile 单片不划算（编译 6 min，且撞显存），只在目录批处理 + `--cache_dit/--cache_vae` 复用时才可能回本。默认保持分块、不编译。
