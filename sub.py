import os, re, sys, glob, shutil, subprocess, tempfile, time, threading, html, requests
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

# ===== 所有敏感信息强制从环境变量读取 =====
def _get_env(var, required=True, default=None):
    val = os.environ.get(var, default)
    if required and val is None:
        sys.exit(f"❌ 错误：未设置环境变量 {var}")
    return val

WEBDAV_BASE = _get_env("WEBDAV_BASE", required=False, default="https://webdav.123pan.cn/webdav").rstrip("/")
WEBDAV_USER = _get_env("WEBDAV_USER", required=True)
WEBDAV_PASS = _get_env("WEBDAV_PASS", required=True)
DEEP        = _get_env("DEEPSEEK_KEY", required=True)
ROOT        = _get_env("ROOT", required=False, default="视频/蔡斯")
ENGINE      = _get_env("ENGINE", required=False, default="deepseek")
DEEP_MODEL  = _get_env("DEEP_MODEL", required=False, default="deepseek-v4-flash")
ASR         = _get_env("ASR", required=False, default="mimo")
REFINE      = _get_env("REFINE", required=False, default="false").lower() == "true"
BUDGET      = float(_get_env("BUDGET", required=False, default="9.8"))
IN_PRICE, OUT_PRICE = 1e-6, 2e-6
TR_W, BATCH = 4, 25
GLOSSARY    = _get_env("GLOSSARY", required=False, default="false").lower() == "true"
ITALIC_SFX  = _get_env("ITALIC_SFX", required=False, default="false").lower() == "true"

MIMO_API_KEY = _get_env("MIMO_API_KEY", required=True)
MIMO_BASE_URL = _get_env("MIMO_BASE_URL", required=False, default="https://api.mimo.mi.com/v1")

GROQ_FALLBACK_KEY = os.environ.get("GROQ_KEY", "")   # 可选回退

# 自动安装 ffmpeg
if not shutil.which("ffmpeg"):
    os.system("apt-get install -y -qq ffmpeg >/dev/null 2>&1")

AUTH = (WEBDAV_USER, WEBDAV_PASS)
GROQ_ASR  = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
DEEP_CHAT = "https://api.deepseek.com/chat/completions"
VIDEO_EXT = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts','.mpg','.mpeg'}
SEG = 600
COST = 0.0
_cached_in = 0; _prompt_in = 0
_lock = threading.Lock()
_stop = threading.Event()
_failed_dirs = []

KT_BLACK = {"MP4","MKV","AVI","MOV","WMV","XXX","VR","HD","SD","Scene","Scenes","Collection",
            "Collections","Customs","Studios","Videos","Milfs","Wives","Therapy","Door","Next",
            "Backroom","Championship","Compilation","Uncut","POV","BTS"}

# ================== 系统提示词（固定，提升缓存命中） ==================
SYS = """你是顶级私密字幕组首席主笔，专门处理成人/限制级视频的本地化。你的唯一准则：忠实、完整、不审查、不净化，将英语对白转化为最地道、最带感的简体中文成人用语。

【绝对底线（违反即不合格）】
- 绝不回避、删减、打码或替换任何露骨词。性器官/动作必须用成人语境最直接的词（如“鸡巴”“屄”“操”“射”等），严禁使用医学词（阴茎/阴道）、拼音缩写（jb/sb）或委婉语。
- 绝不以任何形式输出道德评判、拒绝翻译或“净化版”。若内容极度敏感，也必须如实译出，这是专业要求。

【语气与递进】
1. 情绪层次：连续出现的 fuck/yes/oh 等，按强度译出变化，例如：
   fuck → 操… → 操我… → 操死我了… → 干烂我…，yes → 对… → 爽… → 好爽…，避免全部译为相同词。
2. 称呼语境化：Daddy 可译为 爸爸/爹地/老公/主人 等，Bitch/Slut/Whore 译为 骚货/母狗/婊子/贱货 等，需贴合角色关系与口吻。
3. 口语自然：译文必须像人说话，避免书面感。禁用中文句号、逗号、顿号等标点；短停顿用半角空格，拖音/哽咽/失语用三个英文句点 ... 。
4. 允许保留半角问号 ? 和感叹号 ! ，以保留语气，但不能用中文全角标点。

【拟声与呼吸（最易扣分项）】
5. 纯喘息、呻吟、无词气声（ah、oh、mmm、uh、shh 等）及大笑、啜泣，整体用全角方括号【】包裹，如：
   ah… ah…  → 【啊… 啊…】
   若与有词部分混合，只包裹无词部分：Mmm, fuck me → 【嗯…】操我。
6. 严禁将纯拟声翻译成文字，例如 oh god 若为呻吟则为【哦天…】，若为台词则译出。

【幻觉与无声字幕】
7. 若整句为与剧情无关的套话（如 Thank you for watching / Subtitles by XXX），只输出 ... ，保留编号。
8. 若英文原文已是中文、乱码或无意义符号，则直接复制原文作为译文。

【格式铁律（违反立即作废）】
9. 输出格式：每行必须为 [编号] 译文，编号与译文在同一行，译文内部绝不可换行。编号必须与输入严格一一对应。
10. 只输出译文，禁止任何解释、前言、后记、思考过程或额外字符（包括 markdown 代码块）。
11. 译文尽量简短，去掉不必要的修饰，但绝不丢失原意。

【术语表遵从】
若有提供统一译名表，必须强制使用，保持全片一致。

【正确 vs 错误示例】
输入：
[1] Come here, you little slut.
[2] Oh fuck yes yes yes!
[3] ah… ah… ohh…
[4] Thank you for watching.
[5] こんにちは
错误输出（绝不可为）：
[1] 过来，你这个小荡妇。  （使用了逗号和句号）
[2] 哦，好的好的。        （净化，缺乏递进）
[3] 啊… 啊… 哦…         （未加【】）
[4] 感谢观看             （未处理幻觉）
正确输出：
[1] 过来 你这个小骚货
[2] 噢操 爽 爽 爽!
[3] 【啊… 啊… 哦…】
[4] ...
[5] こんにちは"""

SYS1 = """你是成人字幕翻译，将英语对白译成简体中文。要求：露骨词用最直接的成人用语，不回避、不净化。
纯喘息拟声用【】包裹；幻觉套话只输出 ... ；禁用中文标点，仅用空格断句，拖音用 ... ，可保留半角 ? ! 。
若遇非英语字符，直接保留。仅输出译文，不要编号，不要额外解释。"""

SYS_REFINE = """你是字幕润色专家。拿到“英文原文 / 初译”后，在完全保留原意与尺度的前提下，让译文更地道、更有性张力。
硬性规则：
1. 禁止削弱任何粗口、支配/臣服语气，禁止净化或书面化。
2. 修正生硬表达，使句子自然，但不可改变信息。保留所有【】拟声标记和 ... 拖音。
3. 如有术语表，必须保持一致。
4. 禁用中文句号逗号，可保留半角 ? ! ；短停顿用空格。
5. 输出格式：每行 [编号] 润色后译文，编号同行不换行，编号与行数与输入完全一致。
6. 只输出译文，无任何解释、序言。

示例：
输入：[1] Oh fuck yes... fuck... / 噢操 对… 操…
输出：[1] 操 好爽… 操我…"""

# ============================================================

class LimitExceeded(RuntimeError): pass

def wurl(rel): return WEBDAV_BASE + "/" + quote(rel.strip("/"), safe="/")

def add_cost(u):
    global COST, _cached_in, _prompt_in
    with _lock:
        COST += u.get("prompt_tokens",0)*IN_PRICE + u.get("completion_tokens",0)*OUT_PRICE
        _prompt_in += u.get("prompt_tokens",0)
        _cached_in += (u.get("prompt_tokens_details") or {}).get("cached_tokens",0)
        over = COST >= BUDGET
        if over: _stop.set()
    return over

def post_retry(url, **kw):
    for _ in range(3):
        try:
            r = requests.post(url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(15); continue
            r.raise_for_status(); return r
        except requests.exceptions.HTTPError:
            raise
        except requests.exceptions.RequestException:
            time.sleep(15)
    raise LimitExceeded("多次重试仍失败")

def walk(rel):
    out, norm = [], rel.strip("/")
    r = None
    for attempt in range(3):
        try:
            r = requests.request("PROPFIND", wurl(rel), auth=AUTH, headers={"Depth":"1"}, timeout=180)
            r.raise_for_status(); break
        except Exception as e:
            print(f"  ⚠ 列目录第{attempt+1}次失败 {rel}: {e}"); time.sleep(8); r = None
    if r is None:
        _failed_dirs.append(rel); print(f"  ❌ 目录扫描彻底失败, 该子树本轮被漏: {rel}"); return out
    n_resp = len(re.findall(r"<\w+:response\b", r.text))
    parsed = 0
    for block in re.findall(r"<\w+:response\b.*?</\w+:response>", r.text, re.S):
        hm = re.search(r"<\w+:href>(.*?)</\w+:href>", block, re.S)
        if not hm: continue
        raw = urlparse(html.unescape(unquote(hm.group(1)))).path.split("/webdav",1)[-1]
        relp = raw.strip("/")
        if not relp or relp == norm: continue
        parsed += 1
        is_dir = bool(re.search(r"<\w+:collection\b", block, re.I)) or raw.endswith("/")
        if is_dir and relp != norm: out += walk(relp)
        elif not is_dir: out.append(relp)
    if parsed < n_resp - 2:
        print(f"  ⚠ 目录 {rel} 解析子项{parsed} < 服务器返回{n_resp-1}, 疑似漏块, 记入复查")
        _failed_dirs.append(f"{rel}  (解析{parsed}/返回{n_resp-1})")
    return out

def fmt(t):
    ms=int(round(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def sfx_render(t):
    if ITALIC_SFX:
        return t.replace("【","<i>").replace("】","</i>")
    return t.replace("【","").replace("】","")

def keyterms_for(vp):
    name = os.path.splitext(os.path.basename(vp))[0]
    found = set()
    for m in re.finditer(r"\b([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']+){1,2})\b", name):
        phrase = m.group(1); words = phrase.split()
        if any(w in KT_BLACK for w in words): continue
        if re.fullmatch(r"\d+", words[0]): continue
        found.add(phrase)
    return list(found)[:40]

# ---------- 小米 MiMo 转录 ----------
def transcribe_mimo(path, offset, prompt="", language="en"):
    """
    调用小米 MiMo-V2.5-ASR API，支持 OpenAI 兼容格式和原生格式。
    """
    # 尝试 OpenAI 兼容格式（常见）
    url1 = f"{MIMO_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {MIMO_API_KEY}"}
    files = {"file": (os.path.basename(path), open(path, "rb"), "audio/mpeg")}
    data = {"model": "mimo-v2.5-asr", "language": language, "response_format": "verbose_json"}
    if prompt:
        data["prompt"] = prompt

    try:
        r = requests.post(url1, headers=headers, files=files, data=data, timeout=600)
        if r.status_code == 200:
            j = r.json()
            segments = j.get("segments", [])
            if not segments and "text" in j:  # 简单结果
                segments = [{"start": 0.0, "end": 10.0, "text": j["text"]}]  # 近似
            out = []
            for s in segments:
                text = s.get("text", "").strip()
                if not text: continue
                out.append({
                    "start": float(s.get("start", 0)) + offset,
                    "end": float(s.get("end", 0)) + offset,
                    "text": text
                })
            return out
    except Exception as e:
        print(f"    ⚠ 小米 OpenAI 格式失败: {e}")

    # 尝试小米原生格式（根据文档推测）
    url2 = f"{MIMO_BASE_URL}/speech/recognize"
    files = {"audio": (os.path.basename(path), open(path, "rb"), "audio/mpeg")}
    data2 = {"language": language, "enable_timestamp": "true", "punctuation": "true"}
    if prompt:
        data2["context"] = prompt  # 可能支持热词，但不保证

    try:
        r = requests.post(url2, headers=headers, files=files, data=data2, timeout=600)
        if r.status_code == 200:
            j = r.json()
            # 假设返回格式为 {"result": [{"text": "...", "start": 1.2, "end": 3.4}]}
            items = j.get("result") or j.get("sentences") or []
            if not items:
                # 可能直接是文本
                text = j.get("text") or j.get("data") or ""
                if text:
                    items = [{"text": text, "start": 0.0, "end": 10.0}]
            out = []
            for it in items:
                text = it.get("text", "").strip()
                if not text: continue
                out.append({
                    "start": float(it.get("start", 0)) + offset,
                    "end": float(it.get("end", 0)) + offset,
                    "text": text
                })
            return out
    except Exception as e:
        print(f"    ⚠ 小米原生格式失败: {e}")

    # 全部失败，抛出异常
    raise RuntimeError("小米转录 API 调用失败，请检查 Key 和 URL 是否正确。")

# ---------- Groq Whisper 备选 ----------
def transcribe_whisper(path, offset, prompt=""):
    data = {"model": "whisper-large-v3", "language": "en", "response_format": "verbose_json", "temperature": 0.0}
    if prompt: data["prompt"] = prompt
    with open(path, "rb") as f:
        r = post_retry(GROQ_ASR, headers={"Authorization": f"Bearer {GROQ_FALLBACK_KEY}"},
            data=data, files={"file": (os.path.basename(path), f, "audio/mpeg")}, timeout=900)
    out, prev = [], None
    for s in r.json().get("segments", []):
        if s.get("no_speech_prob", 0) > 0.6 or s.get("avg_logprob", 0) < -0.8: continue
        t = (s.get("text") or "").strip()
        if not t or len(t) < 2: continue
        if t == prev and len(t) > 30: continue
        prev = t
        out.append({"start": float(s.get("start", 0)) + offset, "end": float(s.get("end", 0)) + offset, "text": t})
    return out

def transcribe(path, offset, prompt="", kt=None):
    """
    根据 ASR 配置选择转录引擎。
    """
    if ASR == "mimo":
        try:
            return transcribe_mimo(path, offset, prompt)
        except Exception as e:
            print(f"  ⚠ 小米转录失败，回退到 Whisper: {e}")
            return transcribe_whisper(path, offset, prompt)
    else:
        return transcribe_whisper(path, offset, prompt)

def dl(vp, path):
    with requests.get(wurl(vp), auth=AUTH, stream=True, timeout=3600) as r:
        r.raise_for_status(); total=int(r.headers.get("content-length",0)); got=0
        with open(path,"wb") as f:
            for ch in r.iter_content(1<<20):
                f.write(ch); got+=len(ch)
                if total: print(f"\r    下载 {got/1e6:.0f}/{total/1e6:.0f}MB",end="")
    print()

# 预下载下一个视频（后台线程）
_pf = {}
def start_pf(idx, vp):
    path=f"/tmp/_pf_{idx}.mp4"; evt=threading.Event(); rec={"evt":evt,"path":path,"err":None}; _pf[idx]=rec
    def _t():
        try: dl(vp,path)
        except Exception as e: rec["err"]=e
        finally: evt.set()
    threading.Thread(target=_t, daemon=True).start()
def take_pf(idx):
    rec=_pf.get(idx)
    if not rec: return None
    rec["evt"].wait()
    return None if rec["err"] else rec["path"]

def _chat(messages):
    if _stop.is_set(): raise LimitExceeded("已超预算, 停止")
    if ENGINE == "deepseek":
        for extra in [{"thinking":{"type":"disabled"},"temperature":0.2}, {"temperature":0.2}, {}]:
            body = {"model": DEEP_MODEL, "messages": messages}; body.update(extra)
            try:
                r = post_retry(DEEP_CHAT, headers={"Authorization":f"Bearer {DEEP}","Content-Type":"application/json"}, json=body, timeout=300)
                u = r.json().get("usage", {})
                if (u.get("completion_tokens_details") or {}).get("reasoning_tokens",0):
                    print("    ⚠ 含思考token, 成本或略高于估计")
                if add_cost(u): raise LimitExceeded(f"已达¥{COST:.2f}, 自动停")
                return r.json()["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 400: continue
                raise
        raise RuntimeError("deepseek 调用失败")
    r = post_retry(GROQ_CHAT, headers={"Authorization":f"Bearer {GROQ_FALLBACK_KEY}","Content-Type":"application/json"},
        json={"model":"llama-3.3-70b-versatile","temperature":0.2,"messages":messages}, timeout=300)
    return r.json()["choices"][0]["message"]["content"]

def _parse_batch(txt, n):
    res={}
    for ln in txt.splitlines():
        m=re.match(r"\s*\[(\d+)\]\s*(.*)",ln)
        if m: res[int(m.group(1))]=m.group(2).strip()
    return [res.get(i+1) for i in range(n)]

def tr_batch(texts, ctx=""):
    body="\n".join(f"[{i+1}] {x}" for i,x in enumerate(texts))
    user=(ctx+body) if ctx else body
    txt=_chat([{"role":"system","content":SYS},{"role":"user","content":user}])
    return _parse_batch(txt, len(texts))

def tr_one(x):
    return _chat([{"role":"system","content":SYS1},{"role":"user","content":x}]).strip()

def extract_glossary(alls):
    if not (GLOSSARY and alls): return ""
    text=" ".join(s["text"] for s in alls)
    if len(text)>60000: text=text[:60000]
    try:
        out=_chat([{"role":"system","content":"你是术语提取器。只输出名词表，不翻译整段，无额外文字。"},
                   {"role":"user","content":"从以下英文字幕提取人名/专有名词/反复出现的称呼与术语，给统一简体中文译法，每行 英文=中文，按出现顺序去重，无则只输出 NONE：\n"+text}])
    except Exception as e:
        print("  glossary提取失败,降级空表:",e); return ""
    lines=[ln.strip() for ln in out.splitlines() if "=" in ln and ln.strip().upper()!="NONE"]
    g="\n".join(lines[:80])
    if g: print(f"  专名表 {len(lines)} 条")
    return g

def _run_batches(segs, worker, label):
    n=len(segs); res=[""]*n; batches=list(range(0,n,BATCH))
    if not batches: return res
    def ctx_for(start):
        if start==0: return ""
        return "【前文英文, 仅供理解语境, 勿翻译】\n"+" ".join(s["text"] for s in segs[max(0,start-BATCH):start])+"\n【待译字幕】\n"
    def job(start):
        if _stop.is_set(): return
        try: worker(start, ctx_for(start), segs[start:start+BATCH], res)
        except LimitExceeded: pass
    job(batches[0])  # 预热第一批
    if len(batches) > 1 and not _stop.is_set():
        with ThreadPoolExecutor(max_workers=TR_W) as ex:
            futs=[ex.submit(job,b) for b in batches[1:]]
            for f in futs:
                try: f.result()
                except LimitExceeded: pass
    print(f"    {label} {sum(1 for x in res if x)}/{n}  累计{'¥%.2f'%COST if ENGINE=='deepseek' else '免费'}")
    return res

def _do_translate(start, ctx, texts, res):
    part=None
    try: part=tr_batch(texts, ctx)
    except LimitExceeded: raise
    except Exception as e: print("    批量失败,全逐行:",e)
    if part is None: part=[None]*len(texts)
    for j,t in enumerate(texts):
        if _stop.is_set(): part[j]=part[j] or t; continue
        if part[j] is None:
            try: part[j]=tr_one(t)
            except LimitExceeded: raise
            except Exception as e: print("    单行失败:",e); part[j]=t
    for j,t in enumerate(part): res[start+j]=t

def _do_refine(start, ctx, segs_block, res):
    init=res[start:start+len(segs_block)]
    body="\n".join(f"[{i+1}] {segs_block[i]['text']} / {init[i]}" for i in range(len(segs_block)))
    try:
        pr=_parse_batch(_chat([{"role":"system","content":SYS_REFINE},{"role":"user","content":body}]), len(segs_block))
        for j,t in enumerate(pr):
            if t: res[start+j]=t
    except LimitExceeded: raise
    except Exception as e: print("    润色失败保留初译:",e)

def translate_all(segs):
    res=_run_batches(segs, _do_translate, "翻译")
    if REFINE and not _stop.is_set():
        print("    润色中(REFINE)..."); res=_run_batches(segs, _do_refine, "润色")
    return res

# ========== 智能字幕拆分 ==========
def split_subtitle(text, start, end, max_len=25):
    if not text:
        return [(text, start, end)]
    separators = r'[。！？，、；：\.,;!?]'
    parts = re.split(separators, text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        parts = text.split()
    merged = []
    for p in parts:
        if not merged:
            merged.append(p)
        else:
            if len(merged[-1]) + len(p) + 1 <= max_len:
                merged[-1] += " " + p
            else:
                merged.append(p)
    if len(merged) == 1 and len(merged[0]) > max_len:
        words = merged[0].split()
        merged = []
        cur = ""
        for w in words:
            if len(cur) + len(w) + 1 <= max_len:
                cur = cur + " " + w if cur else w
            else:
                merged.append(cur)
                cur = w
        if cur:
            merged.append(cur)
    total = end - start
    segs = []
    for i, part in enumerate(merged):
        seg_start = start + i * total / len(merged)
        seg_end = start + (i + 1) * total / len(merged)
        segs.append((part, seg_start, seg_end))
    return segs

def process_local(local, vp, srt_rel):
    name = os.path.splitext(os.path.basename(vp))[0]
    srt_local = f"/tmp/{name}.srt"
    tmp = tempfile.mkdtemp()
    kt = keyterms_for(vp) if GLOSSARY else []
    c0, p0 = _cached_in, _prompt_in
    try:
        print("  抽音频+切片...")
        subprocess.run(["ffmpeg","-y","-i",local,"-vn","-ac","1","-ar","16000","-b:a","64k",
            "-f","segment","-segment_time",str(SEG),"-c:a","libmp3lame",os.path.join(tmp,"c_%03d.mp3")],
            check=True, capture_output=True)
        chunks = sorted(glob.glob(os.path.join(tmp, "c_*.mp3")))
        print(f"  共{len(chunks)}片, 转写[MiMo-V2.5-ASR]" + (f", keyterm {len(kt)} 个" if kt else "") + "...")
        alls = []
        last_prompt = ""
        for idx, c in enumerate(chunks):
            if _stop.is_set():
                break
            print(f"  转写{idx+1}/{len(chunks)}...")
            segs = transcribe(c, idx * SEG, last_prompt, kt)
            alls.extend(segs)
            last_prompt = " ".join(s["text"] for s in segs[-6:])[-300:]
        print(f"  转写{len(alls)}句")
        g = extract_glossary(alls) if GLOSSARY else ""
        print(f"  翻译(并发{TR_W}, 预热缓存)...")
        trs = translate_all(alls) if alls else []
        
        with open(srt_local, "w", encoding="utf-8") as f:
            sub_idx = 1
            for s, t in zip(alls, trs):
                text = sfx_render(t)
                sub_segs = split_subtitle(text, s['start'], s['end'], max_len=25)
                for part_text, part_start, part_end in sub_segs:
                    f.write(f"{sub_idx}\n{fmt(part_start)} --> {fmt(part_end)}\n{part_text}\n\n")
                    sub_idx += 1
        
        print("  写回123云盘...")
        with open(srt_local, "rb") as f:
            requests.put(wurl(srt_rel), auth=AUTH, data=f.read(), timeout=120).raise_for_status()
    finally:
        try:
            os.remove(local)
        except:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    dc, dp = _cached_in - c0, _prompt_in - p0
    if dp > 0:
        print(f"  📈 本视频缓存命中 {dc/dp:.0%}  (命中{dc}/输入{dp} token)")

# ================= 主流程 =================
if __name__ == "__main__":
    print(f"🚀 使用小米 MiMo-V2.5-ASR 转录 | 翻译={DEEP_MODEL} | 并发={TR_W} | 批次={BATCH}")
    print("   (固定系统提示词 + 预热缓存 + 智能字幕拆分)")

    if ENGINE == "deepseek":
        print("自检 DeepSeek...")
        try:
            _chat([{"role":"user","content":"reply OK"}])
            print("  自检通过\n")
        except Exception as e:
            print("\n❌ 自检失败, 检查 DEEPSEEK_KEY:", e)
            sys.exit(1)

    print("扫描目录树...")
    allf = walk(ROOT)
    videos = [f for f in allf if os.path.splitext(f)[1].lower() in VIDEO_EXT]
    srt_set = set(f for f in allf if f.lower().endswith(".srt"))
    todo = [vp for vp in videos if (os.path.splitext(vp)[0]+".srt") not in srt_set]
    print(f"📊 扫描到视频 {len(videos)} 个 | 已完成 {len(videos)-len(todo)} | 待处理 {len(todo)}\n")

    done = 0
    for idx, vp in enumerate(todo):
        if _stop.is_set():
            print("\n⏸ 预算到或手动停止。进度已存。")
            break
        srt_rel = os.path.splitext(vp)[0]+".srt"
        print(f"[{idx+1}/{len(todo)}] {vp}")

        local = take_pf(idx)
        if local is None:
            print("  下载(无预下载)...")
            local = "/tmp/_src.mp4"
            try:
                dl(vp, local)
            except Exception as e:
                print("  ❌ 下载失败, 跳过:", e)
                continue
        else:
            print("  命中预下载✓")
        if idx+1 < len(todo):
            start_pf(idx+1, todo[idx+1])

        try:
            process_local(local, vp, srt_rel)
            done += 1
            print(f"  ✅ 完成（新处理 {done}）")
            time.sleep(2)
        except LimitExceeded as e:
            print("\n⏸", e, "\n→ 进度已存。")
            break
        except Exception as e:
            print("  ❌ 跳过:", e)

    tot_hit = _cached_in/_prompt_in if _prompt_in else 0
    print(f"\n===== 本次新处理 {done}, 剩余 {len(todo)-done} | 全程缓存命中 {tot_hit:.0%} =====")
    if _failed_dirs:
        print("\n⚠ 以下目录扫描异常:")
        for d in _failed_dirs: print("   -", d)
    else:
        print("\n✅ 目录扫描完整。")
    print("剩余>0: 再次运行续跑。")
