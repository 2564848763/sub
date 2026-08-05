import os, re, sys, glob, shutil, subprocess, tempfile, time, threading, html, base64, requests
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

# ===== 全部从 GitHub Secrets 读取 =====
WEBDAV_BASE = os.environ.get("WEBDAV_BASE", "https://webdav.123pan.cn/webdav").rstrip("/")
WEBDAV_USER = os.environ.get("WEBDAV_USER")
WEBDAV_PASS = os.environ.get("WEBDAV_PASS")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
GROQ_KEY = os.environ.get("GROQ_KEY", "")
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "")
ROOT = os.environ.get("ROOT", "视频/蔡斯")
ASR = (os.environ.get("ASR", "mimo") or "mimo").strip().lower()

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
ASR_MODEL = "mimo-v2.5-asr"
DEEP_MODEL = "deepseek-v4-flash"
REFINE = os.environ.get("REFINE", "false").lower() == "true"
BUDGET = float(os.environ.get("BUDGET", "9.8"))
IN_PRICE, OUT_PRICE = 1e-6, 2e-6
TR_W = int(os.environ.get("TR_W", "8")); BATCH = 25
GLOSSARY = os.environ.get("GLOSSARY", "true").lower() == "true"
ITALIC_SFX = os.environ.get("ITALIC_SFX", "false").lower() == "true"
PREFETCH = os.environ.get("PREFETCH", "true").lower() == "true"
PARALLEL_DL = int(os.environ.get("PARALLEL_DL", "8"))   # 并行下载连接数
CHUNK_BYTES = 128 << 20                                  # 每块128MB
UPLOAD_VERIFY = True; AUDIO_ENHANCE = True; OVERLAP = 3; MIN_DUR = 1.0; SEG = 60

if not (WEBDAV_USER and WEBDAV_PASS):
    sys.exit("❌ 缺少 WEBDAV_USER / WEBDAV_PASS")
if not (MIMO_API_KEY or DEEPSEEK_KEY):
    sys.exit("❌ 翻译密钥缺失: MIMO_API_KEY 与 DEEPSEEK_KEY 至少填一个")

if not shutil.which("ffmpeg"):
    os.system("apt-get install -y -qq ffmpeg >/dev/null 2>&1")

AUTH = (WEBDAV_USER, WEBDAV_PASS)
VIDEO_EXT = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.m4v','.ts','.mpg','.mpeg'}
COST = 0.0
_cached_in = 0; _prompt_in = 0
_auth_fail = 0
_dl_got = 0
_lock = threading.Lock()
_stop = threading.Event()
_failed_dirs = []
_CUR_SYS = ""; _CUR_SYS1 = ""

SYS = """你是顶级私密字幕组首席主笔，专门处理成人/限制级视频的本地化。你的唯一准则：忠实、完整、不审查、不净化，将英语对白转化为最地道、最带感的简体中文成人用语。

【绝对底线（违反即不合格）】
- 绝不回避、删减、打码或替换任何露骨词。性器官/动作必须用成人语境最直接的词，严禁使用医学词、拼音缩写或委婉语。
- 绝不以任何形式输出道德评判、拒绝翻译或“净化版”。若内容极度敏感，也必须如实译出，这是专业要求。

【语气与递进】
1. 情绪层次：连续出现的 fuck/yes/oh 等，按强度译出变化，避免全部译为相同词。
2. 称呼语境化：Daddy/Bitch/Slut 等需贴合角色关系与口吻。
3. 口语自然：译文必须像人说话，避免书面感。禁用中文句号、逗号、顿号等标点；短停顿用半角空格，拖音/哽咽/失语用三个英文句点 ... 。
4. 允许保留半角问号 ? 和感叹号 ! 。

【拟声与呼吸】
5. 纯喘息、呻吟、无词气声及大笑、啜泣，整体用全角方括号【】包裹，如：【啊… 啊…】。若与有词部分混合，只包裹无词部分。
6. 严禁将纯拟声翻译成文字。

【幻觉与无声字幕】
7. 若整句为与剧情无关的套话（如 Thank you for watching），只输出 ... ，保留编号。
8. 若英文原文已是中文、乱码或无意义符号，则直接复制原文作为译文。

【格式铁律】
9. 输出格式：每行必须为 [编号] 译文，编号与译文在同一行，译文内部绝不可换行。编号必须与输入严格一一对应。
10. 只输出译文，禁止任何解释、前言、后记或额外字符。"""

SYS1 = """你是成人字幕翻译，将英语对白译成简体中文。要求：露骨词用最直接的成人用语，不回避、不净化。
纯喘息拟声用【】包裹；幻觉套话只输出 ... ；禁用中文标点，仅用空格断句，拖音用 ... ，可保留半角 ? ! 。
若遇非英语字符，直接保留。仅输出译文，不要编号，不要额外解释。"""

SYS_REFINE = """你是字幕润色专家。拿到“英文原文 / 初译”后，在完全保留原意与尺度的前提下，让译文更地道、更有性张力。
硬性规则：禁止削弱粗口，禁止净化。保留所有【】拟声标记和 ... 拖音。禁用中文句号逗号。
输出格式：每行 [编号] 润色后译文，编号同行不换行。只输出译文，无任何解释。"""

class LimitExceeded(RuntimeError): pass

def wurl(rel): return WEBDAV_BASE + "/" + quote(rel.strip("/"), safe="/")

def note_auth_fail(e):
    global _auth_fail
    st = getattr(getattr(e, "response", None), "status_code", 0)
    if st in (401, 403):
        with _lock:
            _auth_fail += 1
            if _auth_fail >= 3: _stop.set()
        return True
    return False

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

def req_retry(method, url, **kw):
    for _ in range(3):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(15); continue
            r.raise_for_status(); return r
        except requests.exceptions.RequestException:
            time.sleep(15)
    raise LimitExceeded("多次请求失败")

def walk(rel):
    out, norm = [], rel.strip("/")
    r = None
    for attempt in range(3):
        try:
            r = req_retry("PROPFIND", wurl(rel), auth=AUTH, headers={"Depth":"1"}, timeout=180)
            break
        except Exception as e:
            note_auth_fail(e); print(f"  ⚠ 列目录第{attempt+1}次失败 {rel}: {e}"); time.sleep(8); r = None
    if r is None:
        _failed_dirs.append(rel); print(f"  ❌ 目录扫描彻底失败: {rel}"); return out
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
    return out

def fmt(t):
    ms=int(round(t*1000)); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def sfx_render(t):
    if not isinstance(t, str): t = "" if t is None else str(t)
    if ITALIC_SFX: return t.replace("【","<i>").replace("】","</i>")
    return t

def _cut(s, n):
    if len(s) <= n: return s
    i = s.rfind(" ", 1, n)
    if i > n//2: return s[:i]
    return s[:n]

def wrap_subtitle(text, max_len=42):
    text = (text or "").strip()
    if "\n" in text:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        return "\n".join(lines[:2])
    if len(text) <= max_len: return text
    mid = len(text)//2; best = -1
    for i,ch in enumerate(text):
        if ch == " ":
            if best < 0 or abs(i-mid) < abs(best-mid): best = i
    if best > 0:
        l1, l2 = text[:best].strip(), text[best+1:].strip()
        return _cut(l1, max_len) + "\n" + _cut(l2, max_len)
    return _cut(text, max_len) + "\n" + _cut(text[max_len:], max_len)

def polish_timing(segs):
    for i,s in enumerate(segs):
        if s['end'] <= s['start']: s['end'] = s['start']+MIN_DUR
        if s['end']-s['start'] < MIN_DUR:
            s['end'] = s['start']+MIN_DUR
            if i+1 < len(segs) and s['end'] > segs[i+1]['start']:
                s['end'] = segs[i+1]['start']
    return segs

def merge_segs(existing, new):
    for s in new:
        if any(abs(s['start']-e['start'])<2.0 and s['text']==e['text'] for e in existing[-6:]):
            continue
        if existing and s['start'] < existing[-1]['end'] and len(s['text'])>len(existing[-1]['text']):
            existing[-1]=s
        else:
            existing.append(s)
    return existing

def distribute_text(text, start, end):
    text = (text or "").strip()
    if not text: return []
    parts = re.split(r'(?<=[。！？!?…])\s*|\n+', text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts: parts = [text]
    total = end - start
    totalc = sum(len(p) for p in parts) or 1
    out = []; base = start
    for p in parts:
        frac = len(p)/totalc
        out.append({"start": base, "end": base+total*frac, "text": p})
        base += total*frac
    return out

def probe_dur(local):
    try:
        out = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",local],
                             check=True, capture_output=True, text=True)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0

def _split_long(u, offset):
    txt=u.get("transcript","").strip(); dur=max(u.get("end",0)-u.get("start",0),1e-3)
    if len(txt) <= 110:
        return [{"start":u["start"]+offset,"end":u["end"]+offset,"text":txt}] if txt else []
    parts=re.split(r'(?<=[.!?])\s+|(?<=,)\s+', txt); parts=[p for p in parts if p.strip()]
    if len(parts)<2: parts=[txt]
    totalc=sum(len(p) for p in parts) or 1; base=u["start"]; res=[]
    for p in parts:
        frac=len(p)/totalc; st=base; en=base+dur*frac
        res.append({"start":st+offset,"end":en+offset,"text":p.strip()}); base=en
    return res

# ===== 三种 ASR 后端 =====
def transcribe_mimo(path, offset, dur, prompt="", language="en"):
    url = f"{MIMO_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {MIMO_API_KEY}", "Content-Type": "application/json"}
    with open(path, "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")
    messages = [{"role":"user","content":[{"type":"input_audio","input_audio":{"data":f"data:audio/mpeg;base64,{audio_base64}"}}]}]
    if prompt:
        messages[0]["content"].insert(0, {"type":"text","text":prompt})
    body = {"model": ASR_MODEL, "messages": messages, "asr_options": {"language": language}}
    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=600)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}"); time.sleep(15); continue
            r.raise_for_status()
            j = r.json()
            text = ""
            try: text = j["choices"][0]["message"]["content"]
            except Exception: text = j.get("text","") or j.get("result","")
            if isinstance(text, list):
                text = "".join(t.get("text","") for t in text if isinstance(t,dict))
            return distribute_text(text, offset, offset+dur)
        except requests.exceptions.RequestException as e:
            last = e; time.sleep(15)
    raise RuntimeError(f"小米 ASR 失败: {last}")

def transcribe_whisper(path, offset, prompt=""):
    with open(path, "rb") as f:
        r = post_retry("https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            data={"model":"whisper-large-v3","language":"en","response_format":"verbose_json","temperature":0.0},
            files={"file": (os.path.basename(path), f, "audio/mpeg")}, timeout=900)
    out, prev = [], None
    for s in r.json().get("segments", []):
        if s.get("no_speech_prob",0) > 0.6 or s.get("avg_logprob",0) < -0.8: continue
        t = (s.get("text") or "").strip()
        if not t or len(t) < 2: continue
        if t == prev and len(t) > 30: continue
        prev = t
        out.append({"start": float(s.get("start",0))+offset, "end": float(s.get("end",0))+offset, "text": t})
    return out

def transcribe_deepgram(path, offset):
    params={"model":"nova-3","language":"en","punctuate":"true","smart_format":"true","utterances":"true"}
    with open(path, "rb") as f:
        r = post_retry("https://api.deepgram.com/v1/listen",
            headers={"Authorization": f"Token {DEEPGRAM_KEY}", "Content-Type": "audio/mpeg"},
            params=params, data=f.read(), timeout=900)
    out=[]
    for u in (r.json().get("results",{}).get("utterances") or []):
        if (u.get("confidence") or 1.0) < 0.55: continue
        out += _split_long(u, offset)
    return out

def transcribe(path, offset, dur, prompt=""):
    order = {"mimo":["mimo","whisper","deepgram"],
             "whisper":["whisper","mimo","deepgram"],
             "deepgram":["deepgram","whisper","mimo"]}.get(ASR, ["mimo","whisper","deepgram"])
    last=None
    for b in order:
        try:
            if b=="mimo" and MIMO_API_KEY: return transcribe_mimo(path, offset, dur, prompt)
            if b=="whisper" and GROQ_KEY: return transcribe_whisper(path, offset, prompt)
            if b=="deepgram" and DEEPGRAM_KEY: return transcribe_deepgram(path, offset)
        except Exception as e:
            last=e; print(f"  ⚠ {b} ASR 失败, 换下一个: {e}")
    raise RuntimeError(f"所有ASR均失败: {last}")

# ===== 下载: 并行分块(aria2式) + 单连接兜底 =====
def _bump(g):
    global _dl_got
    with _lock:
        _dl_got += g
        return _dl_got

def _dl_single(vp, path):
    got = os.path.getsize(path) if os.path.exists(path) else 0
    total = 0
    while True:
        if _stop.is_set(): raise LimitExceeded("停止, 放弃下载")
        headers = {"Range": f"bytes={got}-"} if got > 0 else {}
        try:
            with requests.get(wurl(vp), auth=AUTH, stream=True, headers=headers, timeout=3600) as r:
                if r.status_code == 416: break
                r.raise_for_status()
                if r.status_code == 206:
                    total = got + int(r.headers.get("content-length",0)); mode = "ab"
                else:
                    total = int(r.headers.get("content-length",0)); got = 0; mode = "wb"
                with open(path, mode) as f:
                    for ch in r.iter_content(1<<16):
                        f.write(ch); got += len(ch)
                        if total: print(f"\r    下载 {got/1e6:.0f}/{total/1e6:.0f}MB",end="")
            if total == 0 or got >= total: break
        except requests.exceptions.RequestException as e:
            print(f"\n    ⚠ 下载中断({e}), 断点续传...")
            time.sleep(5)
            got = os.path.getsize(path) if os.path.exists(path) else 0
    print()

def _dl_parallel(vp, path, total):
    part = max(CHUNK_BYTES, total // max(1, PARALLEL_DL))
    ranges=[]; s=0
    while s < total:
        e=min(s+part-1, total-1); ranges.append((s,e)); s=e+1
    fd=os.open(path, os.O_CREAT|os.O_WRONLY, 0o644)
    os.ftruncate(fd, total)
    failed=[]; last=[0.0]
    def worker(rng):
        s,e=rng
        for attempt in range(4):
            if _stop.is_set(): return
            off=s
            try:
                with requests.get(wurl(vp), auth=AUTH, stream=True, headers={"Range":f"bytes={s}-{e}"}, timeout=3600) as r:
                    if r.status_code != 206: r.raise_for_status()
                    for ch in r.iter_content(1<<16):
                        os.pwrite(fd, ch, off); off += len(ch)
                        now=_bump(len(ch))
                        if now-last[0] > 200e6 or now >= total:
                            last[0]=now; print(f"\r    并行下载 {now/1e6:.0f}/{total/1e6:.0f}MB",end="")
                if off-1 >= e: return
            except requests.exceptions.RequestException:
                time.sleep(3)
        failed.append(rng)
    with ThreadPoolExecutor(max_workers=min(PARALLEL_DL, len(ranges))) as ex:
        list(ex.map(worker, ranges))
    os.close(fd)
    print()
    if _stop.is_set(): raise LimitExceeded("停止, 放弃下载")
    if failed: raise RuntimeError(f"并行下载{len(failed)}块失败")

def dl(vp, path):
    ok=False; total=0
    try:
        with requests.get(wurl(vp), auth=AUTH, stream=True, headers={"Range":"bytes=0-0"}, timeout=60) as r:
            r.raise_for_status()
            ok = r.status_code == 206
            total = int(r.headers.get("content-range","").split("/")[-1] or 0) if ok else int(r.headers.get("content-length",0))
    except Exception:
        ok, total = False, 0
    if ok and total > CHUNK_BYTES*2 and PARALLEL_DL > 1:
        print(f"  并行下载({min(PARALLEL_DL, max(1,total//CHUNK_BYTES))}连接)...")
        _dl_parallel(vp, path, total)
    else:
        print("  下载(单连接+续传)...")
        _dl_single(vp, path)

_pf = {}
def start_pf(idx, vp):
    path=f"/tmp/_pf_{idx}.mp4"
    try:
        if os.path.exists(path): os.remove(path)
    except: pass
    evt=threading.Event(); rec={"evt":evt,"path":path,"err":None}; _pf[idx]=rec
    def _t():
        try: dl(vp,path)
        except Exception as e: rec["err"]=e
        finally: evt.set()
    threading.Thread(target=_t, daemon=True).start()
def take_pf(idx):
    rec=_pf.pop(idx, None)
    if not rec: return None
    rec["evt"].wait()
    return None if rec["err"] else rec["path"]

# ===== 翻译: 小米优先, DeepSeek兜底 =====
def _chat_mimo(messages):
    body = {"model": MIMO_MODEL, "messages": messages, "max_tokens": 4096, "temperature": 0.2, "stream": False}
    r = post_retry(f"{MIMO_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {MIMO_API_KEY}", "Content-Type": "application/json"}, json=body, timeout=300)
    u = r.json().get("usage", {})
    if add_cost(u): raise LimitExceeded(f"已达¥{COST:.2f}, 自动停")
    return r.json()["choices"][0]["message"]["content"]

def _chat_deepseek(messages):
    for extra in [{"thinking":{"type":"disabled"},"temperature":0.2}, {"temperature":0.2}, {}]:
        body = {"model": DEEP_MODEL, "messages": messages}; body.update(extra)
        try:
            r = post_retry("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}, json=body, timeout=300)
            u = r.json().get("usage", {})
            if add_cost(u): raise LimitExceeded(f"已达¥{COST:.2f}, 自动停")
            return r.json()["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400: continue
            raise
    raise RuntimeError("deepseek 调用失败")

def _chat(messages):
    if _stop.is_set(): raise LimitExceeded("已超预算, 停止")
    last=None
    if MIMO_API_KEY:
        try: return _chat_mimo(messages)
        except Exception as e: last=e
    if DEEPSEEK_KEY:
        try: return _chat_deepseek(messages)
        except Exception as e: last=e
    raise RuntimeError(f"翻译调用失败: {last}")

def _parse_batch(txt, n):
    txt = re.sub(r"^\s*```[a-zA-Z]*\s*", "", txt)
    txt = re.sub(r"\s*```\s*$", "", txt)
    res={}
    for ln in txt.splitlines():
        m=re.match(r"\s*\[(\d+)\]\s*(.*)",ln)
        if m: res[int(m.group(1))]=m.group(2).strip()
    return [res.get(i+1) for i in range(n)]

def tr_batch(texts, ctx=""):
    body="\n".join(f"[{i+1}] {x}" for i,x in enumerate(texts))
    user=(ctx+body) if ctx else body
    with _lock: sysp=_CUR_SYS
    txt=_chat([{"role":"system","content":sysp},{"role":"user","content":user}])
    return _parse_batch(txt, len(texts))

def tr_one(x):
    with _lock: sysp=_CUR_SYS1
    return _chat([{"role":"system","content":sysp},{"role":"user","content":x}]).strip()

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
        lo=max(0,start-BATCH)
        en=" ".join(s["text"] for s in segs[lo:start])
        zh=" / ".join(x for x in res[lo:start] if x)
        head="【前文英文, 仅供理解语境, 勿翻译】\n"+en+"\n"
        if zh: head+="【前文中文译文, 仅供保持语气/称呼一致, 勿重复】\n"+zh+"\n"
        head+="【待译字幕】\n"
        return head
    def job(start):
        if _stop.is_set(): return
        try: worker(start, ctx_for(start), segs[start:start+BATCH], res)
        except LimitExceeded: pass
    job(batches[0])
    print(f"    预热首批完成(写缓存){'，等待就绪' if len(batches)>1 else ''}")
    if len(batches) > 1 and not _stop.is_set():
        time.sleep(1.5)
        with ThreadPoolExecutor(max_workers=TR_W) as ex:
            futs=[ex.submit(job,b) for b in batches[1:]]
            for f in futs:
                try: f.result()
                except LimitExceeded: pass
    print(f"    {label} {sum(1 for x in res if x)}/{n}  累计¥{COST:.2f}")
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

def upload_srt(srt_local, srt_rel):
    with open(srt_local,"rb") as f: data=f.read()
    req_retry("PUT", wurl(srt_rel), auth=AUTH, data=data, timeout=120).raise_for_status()
    if UPLOAD_VERIFY:
        try:
            r=requests.get(wurl(srt_rel), auth=AUTH, stream=True, timeout=60)
            r.raise_for_status()
            remote=int(r.headers.get("content-length",0)); r.close()
            if remote != len(data):
                requests.delete(wurl(srt_rel), auth=AUTH, timeout=60)
                raise RuntimeError(f"上传校验失败(远端{remote}≠本地{len(data)}), 已删残件")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"上传校验失败: {e}")

def process_local(local, vp, srt_rel):
    global _CUR_SYS, _CUR_SYS1
    name=os.path.splitext(os.path.basename(vp))[0]
    srt_local=f"/tmp/{name}.srt"; tmp=tempfile.mkdtemp()
    c0, p0 = _cached_in, _prompt_in
    try:
        print("  抽音频+切片(满血增强+重叠)...")
        dur = probe_dur(local)
        af = "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11" if AUDIO_ENHANCE else "anull"
        chunks=[]; offsets=[]; durs=[]
        if dur>0:
            n = int(dur//SEG) + (1 if dur%SEG>1 else 0)
            for i in range(n):
                ov = OVERLAP if i>0 else 0
                st = max(0.0, i*SEG - ov)
                ln = SEG + ov
                cp = os.path.join(tmp,f"c_{i:03d}.mp3")
                subprocess.run(["ffmpeg","-y","-ss",str(st),"-i",local,"-t",str(ln),"-vn","-ac","1","-ar","16000","-b:a","64k","-af",af,"-c:a","libmp3lame",cp],
                               check=True, capture_output=True)
                chunks.append(cp); offsets.append(st); durs.append(ln)
        else:
            subprocess.run(["ffmpeg","-y","-i",local,"-vn","-ac","1","-ar","16000","-b:a","64k",
                "-f","segment","-segment_time",str(SEG),"-c:a","libmp3lame",os.path.join(tmp,"c_%03d.mp3")],
                check=True, capture_output=True)
            chunks=sorted(glob.glob(os.path.join(tmp,"c_*.mp3")))
            offsets=[i*SEG for i in range(len(chunks))]; durs=[SEG for _ in chunks]
        print(f"  共{len(chunks)}片, 转写[ASR={ASR}]...")
        alls=[]; last_prompt=""
        for idx,c in enumerate(chunks):
            if _stop.is_set(): break
            print(f"  转写{idx+1}/{len(chunks)}...")
            segs=transcribe(c, offsets[idx], durs[idx], last_prompt)
            alls=merge_segs(alls, segs)
            last_prompt=" ".join(s["text"] for s in segs[-6:])[-300:]
        alls=polish_timing(alls)
        print(f"  转写{len(alls)}句(去重+钳制后)")
        g=extract_glossary(alls)
        with _lock:
            _CUR_SYS = SYS + ("\n统一译名表(必须采用, 保持一致)：\n"+g if g else "")
            _CUR_SYS1 = SYS1 + ((" 译名:"+g) if g else "")
        print(f"  翻译(并发{TR_W}, 已预热缓存)...")
        trs=translate_all(alls) if alls else []

        if _stop.is_set():
            print("  ⏸ 停止信号, 放弃写盘, 下次续跑重做")
            raise LimitExceeded("停止, 本视频未写盘")
        if len(trs) != len(alls):
            raise RuntimeError(f"译文{len(trs)}≠原文{len(alls)}, 放弃写盘")

        with open(srt_local,"w",encoding="utf-8") as f:
            for n,(s,t) in enumerate(zip(alls,trs),1):
                f.write(f"{n}\n{fmt(s['start'])} --> {fmt(s['end'])}\n{wrap_subtitle(sfx_render(t if isinstance(t,str) else s['text']))}\n\n")
        print("  写回123云盘(带校验)...")
        upload_srt(srt_local, srt_rel)
    finally:
        try: os.remove(local)
        except: pass
        try: os.remove(srt_local)
        except: pass
        shutil.rmtree(tmp, ignore_errors=True)
    dc, dp = _cached_in-c0, _prompt_in-p0
    if dp > 0: print(f"  📈 本视频缓存命中 {dc/dp:.0%}  (命中{dc}/输入{dp} token)")

if __name__ == "__main__":
    print(f"🚀 GitHub满血版 | ASR={ASR} | 翻译并发={TR_W} | 下载并发={PARALLEL_DL} | 预下载={PREFETCH}")
    print("自检翻译API...")
    try:
        _chat([{"role":"user","content":"reply OK"}]); print("  自检通过\n")
    except Exception as e:
        print("\n❌ 自检失败:", e); sys.exit(1)

    print("扫描目录树...")
    allf = walk(ROOT)
    videos = [f for f in allf if os.path.splitext(f)[1].lower() in VIDEO_EXT]
    srt_set = set(f for f in allf if f.lower().endswith(".srt"))
    todo = [vp for vp in videos if (os.path.splitext(vp)[0]+".srt") not in srt_set]
    print(f"📊 扫描到视频 {len(videos)} 个 | 已完成 {len(videos)-len(todo)} | 待处理 {len(todo)}\n")

    done = 0
    for idx,vp in enumerate(todo):
        if _stop.is_set():
            if _auth_fail >= 3: print("\n⏸ 123连续拒绝(401/403)≥3次, 停止。")
            else: print("\n⏸ 预算到或手动停止。进度已存。")
            break
        srt_rel = os.path.splitext(vp)[0]+".srt"
        print(f"[{idx+1}/{len(todo)}] {vp}")
        local = take_pf(idx) if PREFETCH else None
        if local is None:
            fd, local = tempfile.mkstemp(suffix=".mp4", prefix="src_"); os.close(fd)
            try: dl(vp, local)
            except Exception as e:
                if note_auth_fail(e): print("  ❌ 123拒绝(401/403), 计数+1")
                else: print("  ❌ 下载失败, 跳过:", e)
                try: os.remove(local)
                except: pass
                continue
        else:
            print("  命中预下载✓")
        if PREFETCH and idx+1 < len(todo): start_pf(idx+1, todo[idx+1])
        try:
            process_local(local, vp, srt_rel); done+=1; print(f"  ✅ 完成（新处理 {done}）")
            time.sleep(2)
        except LimitExceeded as e:
            print("\n⏸", e, "\n→ 进度已存。")
            break
        except Exception as e:
            if note_auth_fail(e): print("  ❌ 写回被123拒绝(401/403), 计数+1, 跳过")
            else: print("  ❌ 跳过:", e)
            try: os.remove(local)
            except: pass

    tot_hit = _cached_in/_prompt_in if _prompt_in else 0
    print(f"\n===== 本次新处理 {done}, 剩余 {len(todo)-done} | 全程缓存命中 {tot_hit:.0%} =====")
    if _failed_dirs:
        print("\n⚠ 以下目录本轮扫描异常：")
        for d in _failed_dirs: print("   -", d)
    else:
        print("\n✅ 目录扫描完整。")
    print("剩余>0: 再次触发即续跑, 不丢进度。")
