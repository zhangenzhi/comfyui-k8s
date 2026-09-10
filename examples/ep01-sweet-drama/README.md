# 测试样例：第一集《深夜实验室，魔鬼导师撕了我的论文》（小甜剧，约 60 秒）

纯文本样例，不含任何视频或图片。用来验证整套服务从 ComfyUI 到超算 H3 再回到拼接字幕的完整链路。

| 文件 | 内容 |
|---|---|
| `shot_plan.json` | 5 段分镜计划（每段约 12 s、2–3 个镜头、中文台词、镜头语言、音效、配乐），v2 系统提示词的输出格式 |
| `workflow.api.json` | ComfyUI API 工作流：5 × `H3 Clip Prompt Builder` → 5 × `MiniMax-H3 Generate`（t2va、9:16、768p、50 步、seed 101–105）→ `H3 Video Concat`（硬切、字幕 `aligned`） |
| `run.sh` | 提交工作流并等待出片 |
| `ep01_hook_t2va.txt` | 第 1 段手写的 H3 官方格式提示词（对照 Builder 自动生成的版本） |

```bash
COMFY_URL=https://<host>/test/comfyui COMFY_AUTH='<user>:<password>' ./run.sh
# 集群内：COMFY_URL=http://comfyui-svc:8188 ./run.sh
```

预期：5 段约 25–30 分钟（H3 4×H100，每段约 5 分钟），成片在 ComfyUI `output/h3/examples/ep01/episode01_768p_*.mp4`，
约 58 秒、768×1344、H.264 + AAC 立体声。字幕文本与 `shot_plan.json` 的台词逐字一致，时间戳由音轨强制对齐。
人物外形和画面风格来自 `custom_nodes/comfyui-minimax-h3/bible.json`，改它即可换演员或光线，不用改分镜。
