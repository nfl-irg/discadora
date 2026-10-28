"""Painel da discadora: base com login, permissoes por perfil e auditoria."""
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

BASE = Path(__file__).parent
SEGREDO = (Path.home() / "lab" / "painel_segredo.txt").read_text().strip()
DSN = "dbname=discadora"

# o que cada perfil pode acessar
PERMISSOES = {
    "admin":      {"campanhas", "audios", "relatorios", "acompanhamento", "operador", "usuarios"},
    "supervisor": {"campanhas", "audios", "relatorios", "acompanhamento"},
    "monitoria":  {"audios"},
    "operador":   {"operador"},
    "cliente":    {"relatorios"},
}
MODULOS = [  # (chave, titulo, endereco)
    ("campanhas", "Campanhas", "/campanhas"),
    ("audios", "Áudios", "/audios"),
    ("relatorios", "Relatórios", "/relatorios"),
    ("acompanhamento", "Acompanhamento", "/acompanhamento"),
    ("operador", "Tela do operador", "/operador"),
    ("usuarios", "Usuários", "/usuarios"),
]

app = FastAPI(title="Painel da discadora", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SEGREDO, session_cookie="painel",
                   max_age=8 * 3600, same_site="strict", https_only=True)
templates = Jinja2Templates(directory=BASE / "templates")
hasher = PasswordHasher()


def db():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


def auditar(usuario_id, acao, request, **detalhe):
    with db() as conn:
        conn.execute("INSERT INTO auditoria (usuario_id, acao, detalhe, ip) VALUES (%s, %s, %s, %s)",
                     (usuario_id, acao, Jsonb(detalhe),
                      request.client.host if request.client else None))


class PrecisaLogin(Exception):
    pass


class SemPermissao(Exception):
    pass


@app.exception_handler(PrecisaLogin)
async def tratar_sem_login(request: Request, exc: PrecisaLogin):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(SemPermissao)
async def tratar_sem_permissao(request: Request, exc: SemPermissao):
    return templates.TemplateResponse(request, "negado.html", {"usuario": exc.args[0]},
                                      status_code=403)


def usuario_atual(request: Request, modulo: str | None = None):
    """Devolve o usuario logado; exige login e, se pedido, permissao no modulo."""
    uid = request.session.get("uid")
    if not uid:
        raise PrecisaLogin()
    with db() as conn:
        u = conn.execute("""SELECT id, login, nome, perfil, ramal FROM usuarios
                            WHERE id = %s AND ativo""", (uid,)).fetchone()
    if not u:
        request.session.clear()
        raise PrecisaLogin()
    u["modulos"] = [m for m in MODULOS if m[0] in PERMISSOES[u["perfil"]]]
    if modulo and modulo not in PERMISSOES[u["perfil"]]:
        raise SemPermissao(u)
    return u


@app.get("/login", response_class=HTMLResponse)
def tela_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"erro": None})


@app.post("/login")
def entrar(request: Request, login: str = Form(...), senha: str = Form(...)):
    with db() as conn:
        u = conn.execute("SELECT id, senha_hash FROM usuarios WHERE login = %s AND ativo",
                         (login,)).fetchone()
    ok = False
    if u:
        try:
            ok = hasher.verify(u["senha_hash"], senha)
        except (VerifyMismatchError, InvalidHashError):
            ok = False
    if not ok:
        auditar(u["id"] if u else None, "login_falhou", request, login=login)
        return templates.TemplateResponse(request, "login.html",
                                          {"erro": "Usuário ou senha inválidos."},
                                          status_code=401)
    request.session.clear()
    request.session["uid"] = u["id"]
    auditar(u["id"], "login", request)
    return RedirectResponse("/", status_code=303)


@app.get("/sair")
def sair(request: Request):
    uid = request.session.get("uid")
    request.session.clear()
    if uid:
        auditar(uid, "logout", request)
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    u = usuario_atual(request)
    return templates.TemplateResponse(request, "inicio.html", {"usuario": u})


# ------------------------------ audios ------------------------------
import re, sys
from urllib.parse import urlencode
from fastapi.responses import FileResponse
sys.path.insert(0, str(BASE.parent))
from importar_lista import normalizar_telefone

PASTA_GRAVACOES = Path("/var/spool/asterisk/recording")


@app.get("/audios", response_class=HTMLResponse)
def audios(request: Request, telefone: str = "", campanha: str = "", operador: str = "",
           min_seg: str = "", de: str = "", ate: str = "", pagina: int = 1):
    u = usuario_atual(request, "audios")
    f = {"telefone": telefone, "campanha": campanha, "operador": operador,
         "min_seg": min_seg, "de": de, "ate": ate}
    usados = {k: v for k, v in f.items() if v}            # so os filtros preenchidos
    pagina = max(pagina, 1)
    por_pagina = 50
    # telefone em qualquer formato; no laboratorio, aceita ramais (so digitos)
    tel = (normalizar_telefone(telefone) or re.sub(r"\D", "", telefone)) if telefone else None
    with db() as conn:
        campanhas = conn.execute("""SELECT id, nome FROM campanhas
                                    WHERE tipo = 'call' ORDER BY id DESC""").fetchall()
        operadores = conn.execute("""SELECT login, nome FROM usuarios
                                     WHERE perfil = 'operador' ORDER BY length(login), login""").fetchall()
        resultados = conn.execute("""
            SELECT t.id, t.inicio, coalesce(t.operador_login, t.operador) AS operador,
                   t.telefone, t.conversa_s, t.canal,
                   c.nome AS campanha
            FROM tentativas t JOIN campanhas c ON c.id = t.campanha_id
            WHERE t.atendida_em IS NOT NULL
              AND c.tipo = 'call'
              AND (%(tel)s::text  IS NULL OR t.telefone = %(tel)s)
              AND (%(camp)s::int  IS NULL OR t.campanha_id = %(camp)s)
              AND (%(op)s::text   IS NULL OR t.operador_login = %(op)s)
              AND coalesce(t.conversa_s, 0) >= %(min)s
              AND (%(de)s::date  IS NULL OR t.inicio >= %(de)s::date)
              AND (%(ate)s::date IS NULL OR t.inicio < %(ate)s::date + 1)
            ORDER BY t.inicio DESC
            LIMIT %(lim)s OFFSET %(off)s""",
            {"tel": tel, "camp": int(campanha) if campanha else None,
             "op": operador or None, "min": int(min_seg) if min_seg else 0,
             "de": de or None, "ate": ate or None,
             "lim": por_pagina + 1, "off": (pagina - 1) * por_pagina}).fetchall()
    tem_proxima = len(resultados) > por_pagina          # pediu 1 a mais para saber se ha outra pagina
    resultados = resultados[:por_pagina]
    for r in resultados:
        r["tem_audio"] = (PASTA_GRAVACOES / f"{r['canal']}.wav").exists()
    auditar(u["id"], "audio_busca", request, pagina=pagina, **usados)
    anterior = urlencode({**usados, "pagina": pagina - 1}) if pagina > 1 else None
    proxima = urlencode({**usados, "pagina": pagina + 1}) if tem_proxima else None
    return templates.TemplateResponse(request, "audios.html",
                                      {"usuario": u, "f": f, "filtrou": bool(usados),
                                       "campanhas": campanhas, "operadores": operadores, "resultados": resultados,
                                       "pagina": pagina, "anterior": anterior,
                                       "proxima": proxima})


@app.get("/audios/arquivo/{tentativa_id}")
def audio_arquivo(request: Request, tentativa_id: int, baixar: int = 0):
    u = usuario_atual(request, "audios")
    with db() as conn:
        t = conn.execute("""SELECT canal, telefone, inicio, coalesce(operador_login, operador) AS operador, conversa_s FROM tentativas
                            WHERE id = %s AND atendida_em IS NOT NULL""",
                         (tentativa_id,)).fetchone()
    if not t:
        raise HTTPException(status_code=404)
    arquivo = PASTA_GRAVACOES / f"{t['canal']}.wav"
    if not arquivo.exists():
        raise HTTPException(status_code=404)
    # o player pede o audio em pedacos: registra so o primeiro pedido
    faixa = request.headers.get("range", "")
    if baixar or faixa in ("", "bytes=0-"):
        auditar(u["id"], "audio_baixar" if baixar else "audio_ouvir", request,
                tentativa_id=tentativa_id, telefone=t["telefone"])
    nome = (f"{t['telefone']}_{t['inicio']:%Y%m%d_%H%M%S}_"
            f"op{t['operador'] or 'ura'}_{t['conversa_s'] or 0}s.wav")
    return FileResponse(arquivo, media_type="audio/wav", filename=nome if baixar else None)


# ------------------------------ tela do operador ------------------------------
import httpx
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
ARI = "http://127.0.0.1:8088/ari"
ARI_AUTH = ("discadora", (Path.home() / "lab" / "ari_senha.txt").read_text().strip())


def operador_com_ramal(request: Request):
    u = usuario_atual(request, "operador")
    if not u["ramal"]:
        raise HTTPException(status_code=400, detail="Usuario sem ramal")
    return u


@app.get("/operador", response_class=HTMLResponse)
def tela_operador(request: Request):
    u = usuario_atual(request, "operador")
    return templates.TemplateResponse(request, "operador.html", {"usuario": u})


@app.get("/operador/credenciais")
def credenciais(request: Request):
    """Entrega ao navegador do proprio operador os dados para registrar o ramal dele."""
    u = operador_com_ramal(request)
    with db() as conn:
        r = conn.execute("SELECT senha_sip FROM ramais WHERE ramal = %s", (u["ramal"],)).fetchone()
    host = request.url.hostname
    return {"ramal": u["ramal"], "senha": r["senha_sip"], "dominio": host,
            "wss": f"wss://{host}:8089/ws"}


@app.get("/operador/estado")
def estado_operador(request: Request):
    """O que o operador esta fazendo agora e, se houver chamada, os dados do contato."""
    u = operador_com_ramal(request)
    with db() as conn:
        op = conn.execute("""SELECT estado, tentativa_atual FROM operadores_online
                             WHERE ramal = %s ORDER BY desde DESC LIMIT 1""",
                          (u["ramal"],)).fetchone()
        if not op:
            return {"online": False}
        resp = {"online": True, "estado": op["estado"], "contato": None}
        if op["tentativa_atual"]:
            t = conn.execute("""SELECT t.telefone, t.tabulacao, cc.uf, cc.cidade, cc.dados,
                                       c.nome AS campanha, c.campos_tela, c.questionario_url,
                                       c.tabulacao_ativa, c.tabulacoes
                                FROM tentativas t
                                JOIN campanha_contatos cc ON cc.id = t.campanha_contato_id
                                JOIN campanhas c ON c.id = t.campanha_id
                                WHERE t.id = %s""", (op["tentativa_atual"],)).fetchone()
            if t:
                fontes = {"uf": t["uf"], "cidade": t["cidade"], **(t["dados"] or {})}
                resp["contato"] = {
                    "telefone": t["telefone"],
                    "campanha": t["campanha"],
                    "campos": [{"rotulo": cfg.get("rotulo") or cfg["campo"],
                                "valor": fontes.get(cfg["campo"]) or "—"}
                               for cfg in t["campos_tela"]],
                    "questionario_url": t["questionario_url"],
                    "tabulacao_ativa": t["tabulacao_ativa"],
                    "tabulacoes": t["tabulacoes"],
                    "tabulacao_escolhida": t["tabulacao"],
                }
    return resp


@app.post("/operador/encerrar")
def encerrar_chamada(request: Request):
    """Desliga so o contato; o operador continua na fila."""
    u = operador_com_ramal(request)
    with db() as conn:
        t = conn.execute("""SELECT t.canal FROM operadores_online o
                            JOIN tentativas t ON t.id = o.tentativa_atual
                            WHERE o.ramal = %s AND o.estado = 'em_chamada'""",
                         (u["ramal"],)).fetchone()
    if not t:
        return JSONResponse({"ok": False, "erro": "Nenhuma chamada em andamento"}, status_code=409)
    httpx.delete(f"{ARI}/channels/{t['canal']}", auth=ARI_AUTH, timeout=5)
    return {"ok": True}


# ------------------------------ tabulacao ------------------------------
from pydantic import BaseModel


class Tabulacao(BaseModel):
    codigo: str


@app.post("/operador/tabular")
def tabular(request: Request, dados: Tabulacao):
    u = operador_com_ramal(request)
    with db() as conn:
        op = conn.execute("""SELECT o.canal, o.estado, o.tentativa_atual, t.telefone,
                                    t.campanha_id, c.tabulacoes
                             FROM operadores_online o
                             JOIN tentativas t ON t.id = o.tentativa_atual
                             JOIN campanhas c ON c.id = t.campanha_id
                             WHERE o.ramal = %s AND o.estado IN ('em_chamada', 'tabulando')""",
                          (u["ramal"],)).fetchone()
        if not op:
            return JSONResponse({"ok": False, "erro": "Nenhuma ligação para tabular"},
                                status_code=409)
        opcao = next((x for x in op["tabulacoes"] if x["codigo"] == dados.codigo), None)
        if not opcao:
            return JSONResponse({"ok": False, "erro": "Opção inválida"}, status_code=400)
        conn.execute("UPDATE tentativas SET tabulacao = %s WHERE id = %s",
                     (opcao["codigo"], op["tentativa_atual"]))
        if opcao.get("bloqueia"):
            conn.execute("""INSERT INTO bloqueios (telefone, motivo, origem) VALUES (%s, %s, %s)
                            ON CONFLICT (telefone) DO NOTHING""",
                         (op["telefone"], opcao["nome"],
                          f"campanha {op['campanha_id']}, operador {u['login']}"))
        # confere de novo: a chamada pode ter acabado enquanto o operador escolhia
        estado = conn.execute("SELECT estado FROM operadores_online WHERE canal = %s",
                              (op["canal"],)).fetchone()
    auditar(u["id"], "tabulacao", request, tentativa_id=op["tentativa_atual"],
            codigo=opcao["codigo"])
    if estado and estado["estado"] == "tabulando":
        httpx.post(f"{ARI}/events/user/tabulacao", params={"application": "discadora"},
                   json={"variables": {"canal": op["canal"]}}, auth=ARI_AUTH, timeout=5)
    return {"ok": True}


# ------------------------------ campanhas ------------------------------
import os, subprocess, tempfile
from collections import deque
from datetime import datetime
import relatorio_retorno, exportar_ura

PASTA_LOGS = Path.home() / "lab" / "logs"
REAPROVEITAVEIS = "('atendida', 'respondente', 'invalido', 'atendido em outro telefone')"


def systemctl(*args):
    """Comanda os servicos do usuario (discadora@N, ura@N)."""
    uid = os.getuid()
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{uid}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, env=env, timeout=15)


def servico(c):
    return f"{'ura' if c['tipo'] == 'ura' else 'discadora'}@{int(c['id'])}"


def buscar_campanha(conn, cid):
    c = conn.execute("SELECT * FROM campanhas WHERE id = %s", (cid,)).fetchone()
    if not c:
        raise HTTPException(status_code=404)
    return c


@app.get("/campanhas", response_class=HTMLResponse)
def lista_campanhas(request: Request):
    u = usuario_atual(request, "campanhas")
    with db() as conn:
        campanhas = conn.execute("""
            SELECT c.id, c.nome, c.tipo, c.origem, c.status,
                   count(cc.id) AS total,
                   count(cc.id) FILTER (WHERE cc.status = 'pendente') AS pendentes,
                   (SELECT count(*) FROM tentativas t
                    WHERE t.campanha_id = c.id AND t.inicio >= current_date) AS hoje
            FROM campanhas c
            LEFT JOIN campanha_contatos cc ON cc.campanha_id = c.id
            GROUP BY c.id ORDER BY c.id DESC""").fetchall()
    for c in campanhas:
        c["motor"] = systemctl("is-active", servico(c)).stdout.strip()
    return templates.TemplateResponse(request, "campanhas.html",
                                      {"usuario": u, "campanhas": campanhas})


@app.get("/campanhas/{cid}", response_class=HTMLResponse)
def detalhe_campanha(request: Request, cid: int):
    u = usuario_atual(request, "campanhas")
    with db() as conn:
        c = buscar_campanha(conn, cid)
        numeros = conn.execute("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'pendente')   AS pendentes,
                   count(*) FILTER (WHERE status = 'discando')   AS discando,
                   count(*) FILTER (WHERE status = 'finalizado') AS finalizados,
                   count(*) FILTER (WHERE status = 'finalizado' AND
                         split_part(coalesce(ultimo_resultado, ''), ' (', 1) NOT IN """
                         + REAPROVEITAVEIS + """) AS reciclaveis
            FROM campanha_contatos WHERE campanha_id = %s""", (cid,)).fetchone()
        resultados = conn.execute("""
            SELECT coalesce(split_part(ultimo_resultado, ' (', 1), 'ainda não discado') AS resultado,
                   count(*) AS qtd
            FROM campanha_contatos WHERE campanha_id = %s
            GROUP BY 1 ORDER BY 2 DESC""", (cid,)).fetchall()
        hoje = conn.execute("""SELECT count(*) AS n FROM tentativas
                               WHERE campanha_id = %s AND inicio >= current_date""",
                            (cid,)).fetchone()["n"]
    motor = systemctl("is-active", servico(c)).stdout.strip()
    arquivo_log = PASTA_LOGS / f"{servico(c)}.log"
    log = "".join(deque(arquivo_log.open(encoding="utf-8", errors="replace"), 40)) \
        if arquivo_log.exists() else ""
    return templates.TemplateResponse(request, "campanha.html",
        {"usuario": u, "c": c, "n": numeros, "resultados": resultados, "hoje": hoje,
         "motor": motor, "log": log})


@app.post("/campanhas/{cid}/acao")
def acao_campanha(request: Request, cid: int, acao: str = Form(...)):
    u = usuario_atual(request, "campanhas")
    detalhe = {"campanha": cid}
    with db() as conn:
        c = buscar_campanha(conn, cid)
        if acao == "iniciar":
            conn.execute("UPDATE campanhas SET status = 'ativa' WHERE id = %s", (cid,))
            systemctl("reset-failed", servico(c))
            r = systemctl("start", servico(c))
            detalhe["systemctl"] = r.returncode
        elif acao == "pausar":
            conn.execute("UPDATE campanhas SET status = 'pausada' WHERE id = %s", (cid,))
        elif acao == "encerrar":
            conn.execute("UPDATE campanhas SET status = 'encerrada' WHERE id = %s", (cid,))
            systemctl("stop", servico(c))
        elif acao == "reciclar":
            detalhe["reciclados"] = conn.execute("SELECT reciclar_campanha(%s) AS n",
                                                 (cid,)).fetchone()["n"]
        else:
            raise HTTPException(status_code=400)
    auditar(u["id"], f"campanha_{acao}", request, **detalhe)
    return RedirectResponse(f"/campanhas/{cid}", status_code=303)


@app.get("/campanhas/{cid}/relatorio")
def baixar_relatorio(request: Request, cid: int):
    u = usuario_atual(request, "campanhas")
    destino = Path(tempfile.mkdtemp()) / f"retorno_campanha_{cid}_{datetime.now():%Y%m%d_%H%M}.csv"
    with db() as conn, conn.cursor() as cur:
        c = buscar_campanha(conn, cid)
        if c["origem"] == "lista":
            relatorio_retorno.relatorio_lista(cur, c, destino)
        else:
            relatorio_retorno.relatorio_base(cur, c, destino)
    auditar(u["id"], "relatorio_retorno", request, campanha=cid)
    return FileResponse(destino, media_type="text/csv", filename=destino.name)


@app.get("/campanhas/{cid}/exportar-ura")
def baixar_exportacao_ura(request: Request, cid: int, arquivo: str = "relatorio", todos: int = 0):
    u = usuario_atual(request, "campanhas")
    try:
        pipeline, relatorio = exportar_ura.exportar(cid, bool(todos))
    except SystemExit as e:
        raise HTTPException(status_code=400, detail=str(e))
    alvo = pipeline if arquivo == "pipeline" else relatorio
    auditar(u["id"], "exportacao_ura", request, campanha=cid, arquivo=arquivo)
    return FileResponse(alvo, media_type="text/csv", filename=alvo.name)


# modulos ainda nao construidos: so verifica a permissao e mostra "em construcao"
@app.get("/{modulo}", response_class=HTMLResponse)
def em_construcao(request: Request, modulo: str):
    if modulo not in PERMISSOES["admin"]:
        raise HTTPException(status_code=404)
    u = usuario_atual(request, modulo)
    titulo = next(t for k, t, _ in MODULOS if k == modulo)
    return templates.TemplateResponse(request, "em_construcao.html",
                                      {"usuario": u, "titulo": titulo})
