# Follow-up question: how were the models chosen? / 追问：模型是怎么选的？

> **The author's follow-up question** (wyy511511), asked on 23 Nov 2026 after step 2, before any video was rendered:
>
> 「你的图片模型用了啥啊」 *Which image model did you use?*
>
> 「所以你最开始怎么选的模型」 *So how did you choose the models in the first place?*
>
> **作者的追问**（wyy511511），2026-11-23 在第 2 步之后、渲染任何视频之前提出，原话如上。
> 下面的回答是对这两个问题的回复，保留当时的原样。The answer below is the reply, kept as it stood then.

Production notes / 制作笔记: [step 1, reference sheets](step-1-assets.md) ·
[step 2, first and last frames](step-2-keyframes.md) · [index](README.md)

## English

### The short version

The models were not picked by a head-to-head test. They were narrowed down by constraints, in order: the platform,
then what this episode needs (several reference images, readable text, speech), then price, with evidence from an
earlier project breaking the tie. This note says so plainly, and says what would turn the choice into evidence.

### 1. The platform first

Everything runs on Nunchux Modelverse. Within it, the Nunchux-optimized models (FLUX 3 Klein, Qwen Image, LTX,
MiniMax H3 Fast, Cosmos) come before the partner models (Kling, Seedance, Vidu, Grok Imagine): they are the platform's
own acceleration work, they answer synchronously, and they cost less.

### 2. Images: FLUX 3 Image Klein and Klein Edit

- **Hard requirement: several reference images in one call.** That is where consistency comes from. Klein Edit and
  Qwen Image 2.1 Edit take up to 10; Qwen Image Edit 2511 and 2509 take 3, which rules them out.
- **Klein over Qwen 2.1, on second-hand evidence.** In a same-seed comparison in an earlier project (Freefield),
  Klein kept counts (three apples, five lemons), wrote label text and read a JSON prompt as a scene, where Z-Image,
  Krea 2 and MOSS-Image did not. That project also cites Artificial Analysis' October 2026 ranking, with Klein first
  among open-weights models. Neither was measured on Nunchux for this film.
- **As used:** text to image for the reference sheets (`nunchux-flux.3-image-klein`), and an edit with up to 10
  references for every first and last frame (`nunchux-flux.3-image-klein-edit`), both on the Radical Speed tier.

### 3. Video: MiniMax H3 Fast

| Candidate on Modelverse | Speech and sound | First / last frame | $ per second | Verdict |
|---|---|---|---|---|
| MiniMax H3 Fast | yes | both, or up to 9 references | 0.018–0.030 | chosen |
| LTX-2.5 / 2.3 | no, silent | first only | 0.012–0.040 | out: the episode runs on dialogue |
| Wan 3.0 | yes | both | 0.05–0.20 | 2 to 7 times the price |
| Grok Imagine 1.5 | yes, takes 3 voice references | first only | 0.08–0.25 | too dear; the fallback for voices |
| Kling 4 / V3 | yes | first only | 0.076–0.24 | too dear |

The episode has 112 s of clips and a budget of $6 to $10. Only H3 Fast renders all of it with speech and still
leaves room for re-rolls.

### 4. What was not done

- **No head-to-head on Nunchux.** The image choice rests on another project's evidence.
- **The price list was trusted first.** Edits turned out to be billed on the output and every reference image
  together, about four times the first estimate; the cost model was recalibrated against the account balance after
  step 2.
- **Two H3 risks are untested:** whether one character's voice holds from shot to shot, and whether the off-screen
  narrator stays off screen, with no one in frame mouthing the narration. Step 3 renders one draft clip per voice to
  test both. Fallbacks: Grok Imagine 1.5 with voice references for the dialogue shots, or H3's reference mode with
  a reference audio clip.

### 5. What would make it evidence

- **Images:** three shots (e1s01, the counter wide; e1s08, the phone insert; e1s11, the Driver) on Klein, Qwen Image
  2.1 Edit and Seedream 4.5 with the same prompt, about $0.20.
- **Video:** one dialogue shot on H3 Fast and on Grok Imagine 1.5, about $0.50.
- **The price of knowing:** room for re-rolls drops from about $1.50 to about $0.80.

### 6. Open at this point

- The tier for the final pass: 768p Radical Speed ($3.36, and it gives the render-time figure) or 768p Radical Value
  ($2.69, more re-rolls).
- Whether to spend on the tests in section 5.
- Balance after steps 1 and 2: $5.40 of $6.00.

## 中文

### 一句话

模型不是做完正面对比再选的，是按约束一层层筛出来的：先定平台，再看这一集需要什么（能传多张参考图、
能画准字、能说话），再看价格，最后用之前一个项目的证据在剩下的候选里二选一。这份笔记照实写下来，
也写清楚怎样才能把这个选择变成有证据的结论。

### 1. 先定平台

全部在 Nunchux Modelverse 上跑。平台里优先选 Nunchux 自己加速优化过的模型（FLUX 3 Klein、Qwen Image、LTX、
MiniMax H3 Fast、Cosmos），其次才是合作方模型（Kling、Seedance、Vidu、Grok Imagine）：优化模型是平台自己的
加速成果，同步返回，价格也更低。

### 2. 图片：FLUX 3 Image Klein 和 Klein Edit

- **硬条件：一次能传多张参考图。** 一致性就是从这里来的。Klein Edit 和 Qwen Image 2.1 Edit 最多 10 张；
  Qwen Image Edit 2511 和 2509 只有 3 张，先排除。
- **在 Klein 和 Qwen 2.1 之间选 Klein，依据是二手证据。** 之前的项目（Freefield）做过同种子对比：Klein 数数准
  （三个苹果、五个柠檬），能写对标签上的字，能把 JSON 提示词读成画面，Z-Image、Krea 2、MOSS-Image 都做不到。
  那个项目还引用了 Artificial Analysis 2026 年 10 月的榜单，Klein 在开源权重模型里排第一。这两条都不是这次在
  Nunchux 上测的。
- **实际用法：** 设定图用文字生图（`nunchux-flux.3-image-klein`）；每张首帧和末帧用最多 10 张参考图改图
  （`nunchux-flux.3-image-klein-edit`），都在 Radical Speed 档。

### 3. 视频：MiniMax H3 Fast

| Modelverse 上的候选 | 台词和声音 | 首帧 / 末帧 | 每秒价格（美元） | 结论 |
|---|---|---|---|---|
| MiniMax H3 Fast | 有 | 首尾帧都能用，或最多 9 张参考图 | 0.018–0.030 | 选它 |
| LTX-2.5 / 2.3 | 没有，无声 | 只有首帧 | 0.012–0.040 | 淘汰：这一集靠对白推进 |
| Wan 3.0 | 有 | 首尾帧都能用 | 0.05–0.20 | 贵 2 到 7 倍 |
| Grok Imagine 1.5 | 有，能传 3 个声音参考 | 只有首帧 | 0.08–0.25 | 太贵；声音不稳时的后备 |
| Kling 4 / V3 | 有 | 只有首帧 | 0.076–0.24 | 太贵 |

这一集片段一共 112 秒，预算 $6 到 $10。只有 H3 Fast 能带着台词把全片跑完，还留得出重抽的钱。

### 4. 没做到的

- **没在 Nunchux 上做正面对比。** 图片模型的选择靠的是另一个项目的证据。
- **先信了价格页。** 结果改图是按输出图加所有参考图一起计费，约为最初估算的 4 倍；第 2 步之后按账户余额修正了计费公式。
- **H3 有两个风险还没验证：** 同一个角色的音色在不同镜头之间是否稳定；画外旁白是否真的在画外，画面里没人
  张嘴念旁白。第 3 步每个声音先出一段草稿，专门测这两点。后备方案：对白镜头改用 Grok Imagine 1.5 并传声音参考，
  或者用 H3 的参考模式，传一段参考音频。

### 5. 怎样变成有证据的结论

- **图片：** 三个镜头（e1s01 柜台全景、e1s08 手机特写、e1s11 司机）用同一提示词，在 Klein、Qwen Image 2.1 Edit、
  Seedream 4.5 上各出一版，约 $0.20。
- **视频：** 一个对白镜头在 H3 Fast 和 Grok Imagine 1.5 上各出一版，约 $0.50。
- **代价：** 重抽余量从约 $1.50 降到约 $0.80。

### 6. 此刻还没定的

- 终稿用哪一档：768p Radical Speed（$3.36，能拿到渲染速度的数字），还是 768p Radical Value（$2.69，重抽余量更多）。
- 要不要花钱做第 5 节的对比。
- 第 1、2 步之后余额：$6.00 里还剩 $5.40。
