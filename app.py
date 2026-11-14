"""O Observador: monitor local de serviços da internet."""
import asyncio
import datetime as dt
import html
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# Empacotado (.exe), config e banco ficam ao lado do executável.
CONGELADO = getattr(sys, "frozen", False)
BASE = Path(sys.executable).parent if CONGELADO else Path(__file__).parent
PACOTE = Path(getattr(sys, "_MEIPASS", BASE))
CONFIG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
DB = BASE / "observador.db"
INTERVALO = CONFIG.get("intervalo_segundos", 60)
FALHAS_PARA_ALERTA = CONFIG.get("falhas_para_alerta", 3)
LIMITE_LENTO_MS = CONFIG.get("limite_lento_ms", 3000)
SIMULTANEAS = CONFIG.get("simultaneas", 25)

SEV = {"aguardando": 0, "operacional": 1, "instavel": 2, "indisponivel": 3}
ROTULO = {"aguardando": "aguardando", "operacional": "operacional", "instavel": "instável",
          "indisponivel": "indisponível", "pausado": "pausado"}
MAPA_IND = {"none": "operacional", "minor": "instavel", "major": "instavel", "critical": "indisponivel"}
MAPA_COMP = {"operational": "operacional", "degraded_performance": "instavel",
             "partial_outage": "instavel", "under_maintenance": "instavel",
             "major_outage": "indisponivel"}

ESTADO = {}  # id do serviço -> {"indep": {componente: {...}}, "oficial": {...} ou None}
rede = {"ok": True}


# ---------------------------------------------------------------- banco
@contextmanager
def conexao():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def iniciar_banco():
    with conexao() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS servico(
            id TEXT PRIMARY KEY, nome TEXT, empresa TEXT, categoria TEXT, dominio TEXT,
            status_url TEXT DEFAULT '', leitura TEXT DEFAULT 'nenhuma', nota TEXT DEFAULT '',
            ativo INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS componente(
            servico_id TEXT, nome TEXT, url TEXT, PRIMARY KEY(servico_id, nome));
        CREATE TABLE IF NOT EXISTS hora(
            servico_id TEXT, comp TEXT, h INTEGER, total INTEGER, ok INTEGER,
            PRIMARY KEY(servico_id, comp, h));
        CREATE TABLE IF NOT EXISTS incidente(
            id INTEGER PRIMARY KEY, servico_id TEXT, fonte TEXT, ext_id TEXT, titulo TEXT,
            impacto TEXT, inicio REAL, fim REAL, UNIQUE(servico_id, fonte, ext_id));
        """)
        con.execute("DELETE FROM hora WHERE h < ?", (int(time.time() // 3600) - 24 * 8,))
        # Incidente independente que ficou aberto porque o app foi fechado: encerra agora.
        con.execute("UPDATE incidente SET fim=? WHERE fonte='independente' AND fim IS NULL",
                    (time.time(),))
    semear()


def semear():
    """Acrescenta ao banco os serviços do catálogo que ainda não existem."""
    ext = BASE / "catalogo.yaml"
    arq = ext if ext.exists() else PACOTE / "catalogo.yaml"
    catalogo = yaml.safe_load(arq.read_text(encoding="utf-8"))
    campos = ["id", "nome", "empresa", "dominio", "status_url", "leitura", "nota", "componentes"]
    with conexao() as con:
        for categoria, itens in catalogo.items():
            for it in itens:
                d = dict(zip(campos, it)) if isinstance(it, list) else dict(it)
                url = str(d.get("status_url") or "")
                leitura = "statuspage" if d.get("leitura") in ("sp", "statuspage") and url else "nenhuma"
                cur = con.execute(
                    "INSERT OR IGNORE INTO servico(id,nome,empresa,categoria,dominio,status_url,leitura,nota)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (str(d["id"]), str(d["nome"]), str(d["empresa"]), categoria, str(d["dominio"]),
                     url, leitura, str(d.get("nota") or "")))
                if cur.rowcount:
                    extras = d.get("componentes") or [f"Site|https://{d['dominio']}"]
                    for c in extras:
                        n, u = [x.strip() for x in c.split("|", 1)]
                        con.execute("INSERT OR IGNORE INTO componente VALUES(?,?,?)", (str(d["id"]), n, u))


# ---------------------------------------------------------------- verificações
async def internet_ok():
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, 443), 3)
            w.close()
            return True
        except Exception:
            continue
    return False


async def sondar(client, url):
    """Verificação independente: o servidor responde? Lê só os cabeçalhos, não baixa a página."""
    t = time.perf_counter()
    try:
        async with client.stream("GET", url) as r:
            ms = int((time.perf_counter() - t) * 1000)
            if r.status_code >= 500:
                return "falha", ms, f"HTTP {r.status_code}"
            if ms > LIMITE_LENTO_MS:
                return "lento", ms, "resposta lenta"
            return "ok", ms, f"HTTP {r.status_code}"
    except Exception as e:
        return "falha", None, type(e).__name__


async def ler_oficial(client, base):
    """Informação oficial: página de status no formato Atlassian Statuspage."""
    agora = time.time()
    vazio = {"status": None, "lido": agora, "componentes": [], "total": 0, "incidentes": []}
    try:
        r = await client.get(base.rstrip("/") + "/api/v2/summary.json")
        r.raise_for_status()
        j = r.json()
        comps = [(c["name"], MAPA_COMP.get(c["status"], "instavel"))
                 for c in j.get("components", []) if not c.get("group")]
        comps.sort(key=lambda x: -SEV[x[1]])
        incs = [(i["id"], i["name"], i.get("impact", "none"),
                 dt.datetime.fromisoformat(i["created_at"]).timestamp())
                for i in j.get("incidents", [])]
        return {**vazio, "status": MAPA_IND.get(j["status"]["indicator"], "instavel"),
                "descricao": j["status"]["description"], "erro": None,
                "componentes": comps[:30], "total": len(comps), "incidentes": incs}
    except httpx.HTTPStatusError as e:
        return {**vazio, "erro": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {**vazio, "erro": type(e).__name__}


def notificar(titulo, msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {titulo}: {msg}", flush=True)
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{titulo}"'])
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", titulo, msg])
        elif sys.platform == "win32":
            from plyer import notification  # opcional
            notification.notify(title=titulo, message=msg, timeout=8)
    except Exception:
        pass


def est(sid):
    return ESTADO.setdefault(sid, {"indep": {}, "oficial": None})


def aplicar_indep(con, s, comp, r):
    st, ms, det = r
    agora = time.time()
    c = est(s["id"])["indep"].setdefault(
        comp, {"status": "aguardando", "falhas": 0, "desde": None, "ext": None})
    if st == "falha":
        c["falhas"] += 1
        c["desde"] = c["desde"] or agora
        c["status"] = "indisponivel" if c["falhas"] >= FALHAS_PARA_ALERTA else "instavel"
        if c["falhas"] >= FALHAS_PARA_ALERTA and not c["ext"]:
            c["ext"] = f"{comp}-{int(c['desde'])}"
            con.execute("INSERT OR IGNORE INTO incidente(servico_id,fonte,ext_id,titulo,impacto,inicio)"
                        " VALUES(?,?,?,?,?,?)",
                        (s["id"], "independente", c["ext"], f"Sem resposta em {comp}: {det}",
                         "indisponível", c["desde"]))
            notificar("O Observador", f"{s['nome']} parece indisponível ({comp}: {det}).")
    else:
        if c["ext"]:
            con.execute("UPDATE incidente SET fim=? WHERE servico_id=? AND fonte='independente' AND ext_id=?",
                        (agora, s["id"], c["ext"]))
            notificar("O Observador", f"{s['nome']} voltou a responder ({comp}).")
        c.update(falhas=0, desde=None, ext=None, status="operacional" if st == "ok" else "instavel")
    c["ms"], c["detalhe"] = ms, det
    ok = 0 if st == "falha" else 1
    con.execute("INSERT INTO hora VALUES(?,?,?,1,?) ON CONFLICT(servico_id,comp,h)"
                " DO UPDATE SET total=total+1, ok=ok+?",
                (s["id"], comp, int(agora // 3600), ok, ok))


def aplicar_oficial(con, sid, o):
    est(sid)["oficial"] = o
    if o.get("erro"):
        return  # falha de leitura não é queda do serviço e não mexe nos incidentes
    ativos = []
    for ext, titulo, impacto, inicio in o["incidentes"]:
        ativos.append(ext)
        con.execute("INSERT INTO incidente(servico_id,fonte,ext_id,titulo,impacto,inicio) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(servico_id,fonte,ext_id)"
                    " DO UPDATE SET titulo=excluded.titulo, impacto=excluded.impacto, fim=NULL",
                    (sid, "oficial", ext, titulo, impacto, inicio))
    sql = "UPDATE incidente SET fim=? WHERE servico_id=? AND fonte='oficial' AND fim IS NULL"
    if ativos:
        sql += " AND ext_id NOT IN (%s)" % ",".join("?" * len(ativos))
    con.execute(sql, (time.time(), sid, *ativos))


def carregar():
    with conexao() as con:
        servs = [dict(r) for r in con.execute("SELECT * FROM servico WHERE ativo=1 ORDER BY rowid")]
        comps = {}
        for r in con.execute("SELECT * FROM componente ORDER BY rowid"):
            comps.setdefault(r["servico_id"], []).append(dict(r))
    for s in servs:
        s["comps"] = comps.get(s["id"], [])
    return servs


async def ciclo(client):
    servs = carregar()
    ids = {s["id"] for s in servs}
    for k in list(ESTADO):
        if k not in ids:
            del ESTADO[k]
    sem = asyncio.Semaphore(SIMULTANEAS)

    async def um(s):
        async with sem:
            tarefas = [sondar(client, c["url"]) for c in s["comps"]]
            ler = bool(s["leitura"] == "statuspage" and s["status_url"])
            if ler:
                tarefas.append(ler_oficial(client, s["status_url"]))
            return s, await asyncio.gather(*tarefas), ler

    with conexao() as con:
        for fut in asyncio.as_completed([um(s) for s in servs]):
            s, res, ler = await fut
            e = est(s["id"])
            nomes = [c["nome"] for c in s["comps"]]
            e["indep"] = {k: v for k, v in e["indep"].items() if k in nomes}
            for c, r in zip(s["comps"], res):
                aplicar_indep(con, s, c["nome"], r)
            if ler:
                aplicar_oficial(con, s["id"], res[-1])
            else:
                e["oficial"] = None
            con.commit()


async def laco():
    headers = {"User-Agent": "O-Observador/2.0 (monitor local)"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as c:
        while True:
            rede["ok"] = await internet_ok()
            if rede["ok"]:
                try:
                    await ciclo(c)
                except Exception as e:
                    print("Erro no ciclo:", repr(e), flush=True)
            await asyncio.sleep(INTERVALO)


# ---------------------------------------------------------------- consulta de estado
def situacao(sid):
    e = ESTADO.get(sid)
    if not e:
        return "aguardando"
    sts = [c["status"] for c in e["indep"].values()]
    if e["oficial"] and e["oficial"].get("status"):
        sts.append(e["oficial"]["status"])
    return max(sts, key=SEV.get) if sts else "aguardando"


def motivo(sid):
    e = ESTADO.get(sid) or {"indep": {}, "oficial": None}
    partes = []
    o = e["oficial"]
    if o and o.get("status") and o["status"] != "operacional":
        partes.append(f"Oficial: {o['descricao']}")
    for nome, c in e["indep"].items():
        if c["status"] in ("instavel", "indisponivel"):
            partes.append(f"{nome}: {c.get('detalhe', '')}")
    return "; ".join(partes)


def disponibilidade(sid, comp):
    with conexao() as con:
        r = con.execute("SELECT SUM(total) t, SUM(ok) o FROM hora WHERE servico_id=? AND comp=? AND h>=?",
                        (sid, comp, int(time.time() // 3600) - 23)).fetchone()
    return None if not r["t"] else 100 * r["o"] / r["t"]


def todos_servicos():
    with conexao() as con:
        out = [dict(r) for r in con.execute("SELECT * FROM servico ORDER BY rowid")]
    for s in out:
        s["st"] = situacao(s["id"]) if s["ativo"] else "pausado"
    return out


# ---------------------------------------------------------------- utilidades de texto
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo"]
NUMEROS = ["nenhum", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove",
           "dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis"]


def esc(x):
    return html.escape("" if x is None else str(x))


def sem_acento(t):
    return unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().lower()


def lista_com_e(itens):
    return itens[0] if len(itens) == 1 else ", ".join(itens[:-1]) + " e " + itens[-1]


def numero(n):
    return NUMEROS[n] if n < len(NUMEROS) else str(n)


def duracao(seg):
    seg = int(seg)
    if seg < 60:
        return f"{seg} s"
    if seg < 3600:
        return f"{seg // 60} min"
    if seg < 86400:
        return f"{seg // 3600} h {seg % 3600 // 60:02d} min"
    return f"{seg // 86400} d {seg % 86400 // 3600} h"


def quando(ts):
    d = dt.datetime.fromtimestamp(ts)
    ano = f"/{d.year}" if d.year != dt.datetime.now().year else ""
    return f"{d.day:02d}/{d.month:02d}{ano} às {d:%H:%M}"


def palavra(st):
    return f'<span class="estado st-{st}">{ROTULO[st]}</span>'


def pct(v):
    return "sem medições" if v is None else f"{v:.1f}".replace(".", ",") + "% em 24 h"


# ---------------------------------------------------------------- moldura e estilo
CSS = """
:root{--papel:#ebe8df;--claro:#f4f2ec;--tinta:#16130f;--cinza:#5d584e;--regua:#2a2620;--vermelho:#7d1b17}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);font:18px/1.55 Georgia,"Iowan Old Style","Times New Roman",serif}
main{max-width:1080px;margin:0 auto;padding:28px 24px 48px}
a{color:inherit}
:focus-visible{outline:2px solid var(--tinta);outline-offset:2px}
.titulo-jornal{margin:0;text-align:center;font-weight:700;font-size:clamp(2.8rem,9vw,5.6rem);line-height:1;letter-spacing:.01em;font-variant:small-caps}
.titulo-jornal a{text-decoration:none}
.data{display:flex;justify-content:space-between;gap:16px;margin-top:18px;padding:6px 0;border-top:3px double var(--regua);border-bottom:1px solid var(--regua);font-size:.88rem;font-style:italic}
.manchete{text-align:center;padding:30px 0 26px;border-bottom:1px solid var(--regua)}
.manchete h2{margin:0 auto;max-width:20ch;font-size:clamp(2rem,5vw,3.2rem);line-height:1.08;font-weight:700}
.manchete p{max-width:62ch;margin:14px auto 0;color:var(--cinza);font-style:italic}
.filtro{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:flex-end;padding:14px 0;border-bottom:1px solid var(--regua)}
.filtro .fim{margin-left:auto;display:flex;gap:14px;align-items:baseline}
label{display:flex;flex-direction:column;gap:3px;font-size:.8rem;font-style:italic}
input,select,textarea{font:inherit;font-size:.95rem;color:var(--tinta);background:var(--claro);border:1px solid var(--regua);border-radius:0;padding:6px 8px}
button{font:inherit;font-size:.95rem;background:var(--tinta);color:var(--papel);border:1px solid var(--tinta);border-radius:0;padding:6px 18px;cursor:pointer}
.contagem{margin:10px 0 0;font-size:.85rem;font-style:italic;color:var(--cinza)}
.observacao{padding:18px 0 6px;border-bottom:3px double var(--regua)}
.observacao h3,.colunas h3,.bloco h3{margin:0 0 6px;font-size:1.15rem;font-weight:700;padding-bottom:4px;border-bottom:1px solid var(--regua)}
.observacao p{margin:0 0 10px}
.colunas{column-count:3;column-gap:36px;column-rule:1px solid var(--regua);padding-top:24px}
section{break-inside:avoid;margin-bottom:26px}
ul{list-style:none;margin:0;padding:0}
li{padding:5px 0 4px;border-bottom:1px dotted #8d877a}
.linha{display:flex;align-items:baseline;gap:6px;text-decoration:none}
.linha:hover .nome{text-decoration:underline}
.pontos{flex:1}
.nome{font-weight:700}
.estado{font-size:.92rem}
.st-instavel{font-style:italic}
.st-indisponivel{color:var(--vermelho);font-weight:700;text-decoration:underline}
.st-aguardando,.st-pausado{color:var(--cinza)}
li.indisponivel .nome{color:var(--vermelho)}
.nota{font-size:.82rem;color:var(--cinza);font-style:italic}
.voltar{margin:18px 0 0;font-size:.9rem;font-style:italic}
.ficha{padding:14px 0 22px;text-align:center;border-bottom:1px solid var(--regua)}
.ficha h2{margin:0;font-size:clamp(2rem,5vw,3.2rem);line-height:1.1}
.ficha p{margin:8px 0 0;color:var(--cinza);font-style:italic}
.tres{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--regua)}
.tres>div{padding:18px 20px 16px;border-left:1px solid var(--regua)}
.tres>div:first-child{border-left:0;padding-left:0}
.tres h3{margin:0 0 6px;font-size:1.05rem}
.tres p{margin:6px 0;font-size:.92rem}
.bloco{padding-top:22px}
.vazio{font-style:italic;color:var(--cinza)}
.registro li{font-size:.95rem}
form.cadastro{max-width:640px;margin:22px auto 0;display:flex;flex-direction:column;gap:14px}
form.cadastro label{font-size:.9rem}
form.cadastro .dica{font-size:.78rem;color:var(--cinza)}
.erros{border:1px solid var(--vermelho);color:var(--vermelho);padding:8px 14px;margin-top:18px}
.erros p{margin:2px 0}
.check{flex-direction:row;align-items:center;gap:8px;font-style:normal}
footer{margin-top:34px;padding-top:10px;border-top:3px double var(--regua);font-size:.8rem;color:var(--cinza);display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
footer p{margin:0}
@media(max-width:860px){.colunas{column-count:2}}
@media(max-width:700px){.tres,footer{grid-template-columns:1fr}.tres>div{border-left:0;padding-left:0;border-top:1px solid var(--regua)}.tres>div:first-child{border-top:0}}
@media(max-width:560px){.colunas{column-count:1}.data{flex-direction:column;gap:2px}.filtro .fim{margin-left:0}}
"""


def moldura(corpo, refresh=True, titulo="O Observador"):
    agora = dt.datetime.now()
    data = f"{DIAS[agora.weekday()].capitalize()}, {agora.day} de {MESES[agora.month - 1]} de {agora.year}"
    meta = '<meta http-equiv="refresh" content="30">' if refresh else ""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}<title>{esc(titulo)}</title>
<style>{CSS}</style></head><body><main>
<h1 class="titulo-jornal"><a href="/">O Observador</a></h1>
<div class="data"><span>{data}</span><span>Edição das {agora:%H:%M:%S}</span></div>
{corpo}
<footer>
<p><b>Informação oficial.</b> O que a própria empresa publica na sua página de status. Só é lida
automaticamente quando a página usa o formato Statuspage; nos demais casos o app apenas indica o endereço.</p>
<p><b>Verificação independente.</b> Teste feito por este computador a cada {INTERVALO} segundos.
Mostra se o servidor responde, não se todas as funções do serviço estão funcionando.</p>
<p><b>Relatos de usuários.</b> Este app não coleta relatos, portanto não há dados dessa fonte.</p>
</footer></main></body></html>"""


# ---------------------------------------------------------------- primeira página
def manchete(ativos):
    if not rede["ok"]:
        return ("Sem conexão com a internet",
                "A rede local não alcança os servidores de referência. As verificações ficam suspensas "
                "até a conexão voltar, para não acusar falsas quedas.")
    n = len(ativos)
    fora = [s["nome"] for s in ativos if s["st"] == "indisponivel"]
    inst = [s["nome"] for s in ativos if s["st"] == "instavel"]
    aguard = sum(1 for s in ativos if s["st"] == "aguardando")
    ok = sum(1 for s in ativos if s["st"] == "operacional")
    if aguard == n:
        return "Primeira rodada de verificações em andamento", f"{n} serviços na fila. Os resultados aparecem em instantes."
    resumo = f"{ok} operacionais, {len(inst)} instáveis, {len(fora)} indisponíveis"
    if aguard:
        resumo += f"; {aguard} ainda aguardam a primeira verificação"
    if fora:
        tit = (f"{lista_com_e(fora)} indisponível" if len(fora) == 1 else
               f"{lista_com_e(fora)} indisponíveis" if len(fora) <= 3 else
               f"{numero(len(fora)).capitalize()} serviços indisponíveis")
    elif inst:
        tit = (f"Instabilidade em {lista_com_e(inst)}" if len(inst) <= 3 else
               f"Instabilidade em {numero(len(inst))} serviços")
    else:
        tit = f"Os {n} serviços vigiados respondem normalmente"
    return tit, resumo + "."


def passa(s, cat, sit, busca):
    if sit == "pausado":
        return not s["ativo"] and (not cat or s["categoria"] == cat) and busca_ok(s, busca)
    if not s["ativo"] or (cat and s["categoria"] != cat) or not busca_ok(s, busca):
        return False
    if sit == "problema":
        return s["st"] in ("instavel", "indisponivel")
    return not sit or s["st"] == sit


def busca_ok(s, busca):
    return not busca or sem_acento(busca) in sem_acento(f"{s['nome']} {s['empresa']} {s['dominio']} {s['id']}")


def inicio_html(p):
    cat, sit, busca = p.get("secao", ""), p.get("situacao", ""), p.get("busca", "").strip()
    todos = todos_servicos()
    ativos = [s for s in todos if s["ativo"]]
    cats = {}
    for s in ativos:
        cats[s["categoria"]] = cats.get(s["categoria"], 0) + 1
    vis = [s for s in todos if passa(s, cat, sit, busca)]
    titulo, sub = manchete(ativos)

    opt_cat = '<option value="">Todas</option>' + "".join(
        f'<option value="{esc(c)}"{" selected" if c == cat else ""}>{esc(c)} ({n})</option>'
        for c, n in cats.items())
    sits = [("", "Todas"), ("problema", "Só com problema"), ("operacional", "Operacionais"),
            ("aguardando", "Aguardando"), ("pausado", "Pausados")]
    opt_sit = "".join(f'<option value="{v}"{" selected" if v == sit else ""}>{r}</option>' for v, r in sits)
    filtro = f"""<form class="filtro" method="get" action="/">
<label>Seção<select name="secao">{opt_cat}</select></label>
<label>Situação<select name="situacao">{opt_sit}</select></label>
<label>Busca<input type="search" name="busca" value="{esc(busca)}" placeholder="nome ou domínio"></label>
<div class="fim"><button type="submit">Filtrar</button><a href="/">Limpar</a><a href="/novo">Cadastrar serviço</a></div>
</form>"""
    ativo_filtro = bool(cat or sit or busca)
    contagem = (f'<p class="contagem">{len(vis)} de {len(todos)} serviços correspondem ao filtro.</p>'
                if ativo_filtro else "")

    prob = sorted([s for s in vis if s["st"] in ("instavel", "indisponivel")],
                  key=lambda s: -SEV[s["st"]])
    obs = ""
    if prob:
        itens = "".join(
            f'<p><a href="/servico/{esc(s["id"])}"><b>{esc(s["nome"])}</b></a>, {palavra(s["st"])}. '
            f'<span class="nota">{esc(motivo(s["id"]))}</span></p>' for s in prob)
        obs = f'<div class="observacao"><h3>Em observação</h3>{itens}</div>'

    grupos = {}
    for s in vis:
        grupos.setdefault(s["categoria"], []).append(s)
    if grupos:
        colunas = "".join(
            f'<section><h3>{esc(c)}</h3><ul>' + "".join(
                f'<li class="{s["st"]}"><a class="linha" href="/servico/{esc(s["id"])}">'
                f'<span class="nome">{esc(s["nome"])}</span><span class="pontos"></span>{palavra(s["st"])}</a></li>'
                for s in lista) + "</ul></section>" for c, lista in grupos.items())
        corpo_lista = f'<div class="colunas">{colunas}</div>'
    else:
        corpo_lista = '<p class="vazio" style="text-align:center;padding:30px 0">Nenhum serviço corresponde ao filtro. <a href="/">Limpar filtros</a>.</p>'
    return moldura(f'<div class="manchete"><h2>{esc(titulo)}</h2><p>{esc(sub)}</p></div>'
                   f'{filtro}{contagem}{obs}{corpo_lista}')


# ---------------------------------------------------------------- página do serviço
def detalhe_html(sid):
    with conexao() as con:
        s = con.execute("SELECT * FROM servico WHERE id=?", (sid,)).fetchone()
        if not s:
            return None
        s = dict(s)
        comps = [dict(r) for r in con.execute("SELECT * FROM componente WHERE servico_id=? ORDER BY rowid", (sid,))]
        incs = [dict(r) for r in con.execute(
            "SELECT * FROM incidente WHERE servico_id=? ORDER BY inicio DESC LIMIT 20", (sid,))]
    st = situacao(sid) if s["ativo"] else "pausado"
    e = ESTADO.get(sid) or {"indep": {}, "oficial": None}

    # fonte 1: oficial
    o = e["oficial"]
    link = (f'<a href="{esc(s["status_url"])}" rel="noopener">{esc(s["status_url"])}</a>' if s["status_url"] else "")
    if s["leitura"] == "statuspage" and s["status_url"]:
        if o is None:
            of = '<p class="vazio">Aguardando a primeira leitura.</p>'
        elif o.get("erro"):
            of = (f'<p>Leitura da página de status falhou ({esc(o["erro"])}). Isso não altera a situação do serviço.</p>'
                  f'<p class="nota">{link}</p>')
        else:
            of = (f'<p>{palavra(o["status"])}: {esc(o["descricao"])}</p>'
                  f'<p class="nota">Lido às {dt.datetime.fromtimestamp(o["lido"]):%H:%M:%S} em {link}</p>')
    elif s["status_url"]:
        of = f'<p>Há página oficial de status, mas o app não a lê automaticamente. Consulte:</p><p class="nota">{link}</p>'
    else:
        of = '<p class="vazio">Nenhuma página oficial de status cadastrada.</p>'

    # fonte 2: independente
    if not s["ativo"]:
        ind = '<p class="vazio">Serviço pausado: sem verificações.</p>'
    elif not e["indep"]:
        ind = '<p class="vazio">Aguardando a primeira verificação.</p>'
    else:
        ind = "".join(
            f'<p><b>{esc(n)}</b>: {palavra(c["status"])}'
            f'{"" if c.get("ms") is None else f", {c["ms"]} ms"}<br>'
            f'<span class="nota">{esc(c.get("detalhe", ""))}; {pct(disponibilidade(sid, n))}</span></p>'
            for n, c in e["indep"].items())

    rel = ('<p>Não coletados. Este app não recebe relatos de usuários, então não há dados dessa fonte.</p>')

    # componentes
    li_ind = "".join(
        f'<li><div class="linha"><span class="nome">{esc(c["nome"])}</span><span class="pontos"></span>'
        f'{palavra((e["indep"].get(c["nome"]) or {"status": "aguardando"})["status"])}</div>'
        f'<div class="nota">Verificação independente: {esc(c["url"])}</div></li>' for c in comps)
    li_of = ""
    if o and not o.get("erro") and o["componentes"]:
        li_of = "".join(
            f'<li><div class="linha"><span class="nome">{esc(n)}</span><span class="pontos"></span>{palavra(x)}</div>'
            f'<div class="nota">Informação oficial</div></li>' for n, x in o["componentes"])
        if o["total"] > len(o["componentes"]):
            li_of += f'<li class="nota">Mostrando {len(o["componentes"])} de {o["total"]} componentes publicados.</li>'
    comp_html = f'<div class="bloco"><h3>Componentes monitorados</h3><ul>{li_ind}{li_of}</ul></div>'

    # incidentes
    if incs:
        fonte = {"oficial": "informação oficial", "independente": "verificação independente"}
        itens = ""
        for i in incs:
            fim = f"encerrado após {duracao(i['fim'] - i['inicio'])}" if i["fim"] else "em andamento"
            itens += (f'<li><b>{esc(i["titulo"])}</b><br><span class="nota">{quando(i["inicio"])}, {fim}. '
                      f'Fonte: {fonte[i["fonte"]]}.</span></li>')
        hist = f'<ul class="registro">{itens}</ul>'
    else:
        hist = '<p class="vazio">Nenhum incidente observado desde que o Observador começou a vigiar este serviço.</p>'
    hist += ('<p class="nota">O histórico começa quando o app começa a observar. O fim de incidentes oficiais é o '
             'momento em que o app viu a página de status encerrá-los.</p>')

    nota = f'<p class="nota">{esc(s["nota"])}</p>' if s["nota"] else ""
    return moldura(f"""<p class="voltar"><a href="/">Voltar à primeira página</a></p>
<div class="ficha"><h2>{esc(s["nome"])}</h2>
<p>{esc(s["empresa"])}, seção {esc(s["categoria"])}. Domínio: <a href="https://{esc(s["dominio"])}" rel="noopener">{esc(s["dominio"])}</a>.
Situação geral: {palavra(st)}. Identificador: {esc(s["id"])}.</p>{nota}
<p><a href="/servico/{esc(sid)}/editar">Editar cadastro</a></p></div>
<div class="tres"><div><h3>Informação oficial</h3>{of}</div>
<div><h3>Verificação independente</h3>{ind}</div>
<div><h3>Relatos de usuários</h3>{rel}</div></div>
{comp_html}<div class="bloco"><h3>Histórico de incidentes</h3>{hist}</div>""", titulo=f"{s['nome']} - O Observador")


# ---------------------------------------------------------------- cadastro e edição
def formulario(d, erros, novo, sid=None):
    with conexao() as con:
        cats = [r[0] for r in con.execute("SELECT DISTINCT categoria FROM servico ORDER BY categoria")]
    lista = "".join(f'<option value="{esc(c)}">' for c in cats)
    sel = lambda v: ' selected' if d.get("leitura", "nenhuma") == v else ""
    erros_html = ('<div class="erros">' + "".join(f"<p>{esc(x)}</p>" for x in erros) + "</div>") if erros else ""
    acao = "/novo" if novo else f"/servico/{esc(sid)}/editar"
    pausa = ("" if novo else
             f'<label class="check"><input type="checkbox" name="ativo" value="1"{" checked" if d.get("ativo") else ""}> '
             'Monitorar este serviço (desmarque para pausar as verificações)</label>')
    h = "Cadastrar serviço" if novo else f"Editar {d.get('nome', '')}"
    return f"""<p class="voltar"><a href="{'/' if novo else '/servico/' + esc(sid)}">Cancelar e voltar</a></p>
<div class="ficha"><h2>{esc(h)}</h2></div>{erros_html}
<form class="cadastro" method="post" action="{acao}">
<label>Nome<input name="nome" value="{esc(d.get('nome'))}" required></label>
<label>Empresa responsável<input name="empresa" value="{esc(d.get('empresa'))}"></label>
<label>Categoria<input name="categoria" list="cats" value="{esc(d.get('categoria'))}" required>
<datalist id="cats">{lista}</datalist><span class="dica">Escolha uma existente ou digite uma nova.</span></label>
<label>Domínio oficial<input name="dominio" value="{esc(d.get('dominio'))}" placeholder="exemplo.com.br" required></label>
<label>Página oficial de status (opcional)<input name="status_url" value="{esc(d.get('status_url'))}" placeholder="https://status.exemplo.com"></label>
<label>Leitura da página de status<select name="leitura">
<option value="nenhuma"{sel('nenhuma')}>Apenas mostrar o link</option>
<option value="statuspage"{sel('statuspage')}>Ler automaticamente (formato Statuspage)</option></select>
<span class="dica">Use a leitura automática só se o endereço responder em /api/v2/summary.json.</span></label>
<label>Componentes verificados<textarea name="componentes" rows="4">{esc(d.get('componentes'))}</textarea>
<span class="dica">Um por linha, no formato Nome | URL. Se ficar vazio, usa o domínio como "Site".</span></label>
<label>Observação (opcional)<textarea name="nota" rows="2">{esc(d.get('nota'))}</textarea></label>
{pausa}<div><button type="submit">Salvar</button></div></form>"""


def validar(d):
    erros = []
    nome, categoria = d.get("nome", ""), d.get("categoria", "")
    dominio = re.sub(r"^https?://", "", d.get("dominio", "").lower()).split("/")[0]
    status_url = d.get("status_url", "")
    if not nome:
        erros.append("Informe o nome do serviço.")
    if not categoria:
        erros.append("Informe a categoria.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", dominio):
        erros.append("Informe um domínio válido, como exemplo.com.br.")
    if status_url and not re.match(r"https?://\S+$", status_url):
        erros.append("A página de status deve começar com http:// ou https://.")
    comps = []
    for linha in d.get("componentes", "").splitlines():
        if not linha.strip():
            continue
        n, _, u = linha.partition("|")
        if not n.strip() or not re.match(r"https?://\S+$", u.strip()):
            erros.append(f'Componente inválido: "{linha.strip()}". Use Nome | https://endereco.')
        else:
            comps.append((n.strip(), u.strip()))
    if not comps and dominio:
        comps = [("Site", f"https://{dominio}")]
    leitura = "statuspage" if d.get("leitura") == "statuspage" and status_url else "nenhuma"
    return {"nome": nome, "empresa": d.get("empresa", ""), "categoria": categoria, "dominio": dominio,
            "status_url": status_url, "leitura": leitura, "nota": d.get("nota", ""),
            "ativo": 1 if d.get("ativo") else 0, "comps": comps}, erros


def novo_id(nome):
    base = re.sub(r"[^a-z0-9]+", "-", sem_acento(nome)).strip("-") or "servico"
    with conexao() as con:
        sid, i = base, 2
        while con.execute("SELECT 1 FROM servico WHERE id=?", (sid,)).fetchone():
            sid, i = f"{base}-{i}", i + 1
    return sid


def gravar(sid, v, criar):
    with conexao() as con:
        if criar:
            con.execute("INSERT INTO servico(id,nome,empresa,categoria,dominio,status_url,leitura,nota,ativo)"
                        " VALUES(?,?,?,?,?,?,?,?,1)",
                        (sid, v["nome"], v["empresa"], v["categoria"], v["dominio"], v["status_url"],
                         v["leitura"], v["nota"]))
        else:
            con.execute("UPDATE servico SET nome=?,empresa=?,categoria=?,dominio=?,status_url=?,leitura=?,"
                        "nota=?,ativo=? WHERE id=?",
                        (v["nome"], v["empresa"], v["categoria"], v["dominio"], v["status_url"],
                         v["leitura"], v["nota"], v["ativo"], sid))
        con.execute("DELETE FROM componente WHERE servico_id=?", (sid,))
        for n, u in v["comps"]:
            con.execute("INSERT OR REPLACE INTO componente VALUES(?,?,?)", (sid, n, u))


async def dados_form(request):
    corpo = (await request.body()).decode("utf-8")
    return {k: v[0].strip() for k, v in parse_qs(corpo, keep_blank_values=True).items()}


# ---------------------------------------------------------------- servidor
@asynccontextmanager
async def vida(app):
    iniciar_banco()
    tarefa = asyncio.create_task(laco())
    yield
    tarefa.cancel()


app = FastAPI(lifespan=vida, title="O Observador")


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    return inicio_html(request.query_params)


@app.get("/servico/{sid}", response_class=HTMLResponse)
def servico(sid: str):
    pag = detalhe_html(sid)
    return HTMLResponse(pag) if pag else HTMLResponse(
        moldura('<p class="vazio" style="text-align:center;padding:40px 0">Serviço não encontrado. '
                '<a href="/">Voltar à primeira página</a>.</p>', refresh=False), status_code=404)


@app.get("/novo", response_class=HTMLResponse)
def novo_form():
    return moldura(formulario({"leitura": "nenhuma"}, [], True), refresh=False, titulo="Cadastrar serviço")


@app.post("/novo")
async def novo_post(request: Request):
    d = await dados_form(request)
    v, erros = validar(d)
    if erros:
        return HTMLResponse(moldura(formulario(d, erros, True), refresh=False, titulo="Cadastrar serviço"))
    sid = novo_id(v["nome"])
    gravar(sid, v, True)
    return RedirectResponse(f"/servico/{sid}", status_code=303)


@app.get("/servico/{sid}/editar", response_class=HTMLResponse)
def editar_form(sid: str):
    with conexao() as con:
        s = con.execute("SELECT * FROM servico WHERE id=?", (sid,)).fetchone()
        if not s:
            return RedirectResponse("/", status_code=303)
        d = dict(s)
        d["componentes"] = "\n".join(f"{r['nome']} | {r['url']}" for r in con.execute(
            "SELECT * FROM componente WHERE servico_id=? ORDER BY rowid", (sid,)))
    return moldura(formulario(d, [], False, sid), refresh=False, titulo=f"Editar {d['nome']}")


@app.post("/servico/{sid}/editar")
async def editar_post(sid: str, request: Request):
    d = await dados_form(request)
    v, erros = validar(d)
    if erros:
        d["nome"] = d.get("nome") or sid
        return HTMLResponse(moldura(formulario(d, erros, False, sid), refresh=False))
    gravar(sid, v, False)
    return RedirectResponse(f"/servico/{sid}", status_code=303)


@app.get("/api/estado")
def api_estado():
    return JSONResponse({"rede_ok": rede["ok"], "servicos": [
        {"id": s["id"], "nome": s["nome"], "categoria": s["categoria"], "situacao": s["st"]}
        for s in todos_servicos()]})


if __name__ == "__main__":
    if sys.stdout is None:  # executável sem janela de console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    porta = CONFIG.get("porta", 8765)
    threading.Timer(2, lambda: webbrowser.open(f"http://127.0.0.1:{porta}")).start()
    uvicorn.run(app, host="127.0.0.1", port=porta, log_level="warning", log_config=None)
