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
    "monitoria":  {"audios", "acompanhamento"},
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
        op = conn.execute("""SELECT estado, tentativa_atual, pausa_solicitada,
                                    extract(epoch FROM now() - estado_desde)::int AS segundos
                             FROM operadores_online
                             WHERE ramal = %s ORDER BY desde DESC LIMIT 1""",
                          (u["ramal"],)).fetchone()
        if not op:
            return {"online": False}
        resp = {"online": True, "estado": op["estado"], "contato": None,
                "pausa_solicitada": op["pausa_solicitada"], "segundos": op["segundos"]}
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
    # UFs e cidades de cada campanha, para os filtros da lista
    with db() as conn:
        regioes = {r["campanha_id"]: r for r in conn.execute("""
            SELECT campanha_id,
                   array_remove(array_agg(DISTINCT upper(dados->>'uf')), NULL) AS ufs,
                   array_remove(array_agg(DISTINCT upper(dados->>'cidade')), NULL) AS cidades
            FROM campanha_contatos GROUP BY 1""").fetchall()}
    for c in campanhas:
        r = regioes.get(c["id"]) or {}
        c["ufs"], c["cidades"] = r.get("ufs") or [], r.get("cidades") or []
    return templates.TemplateResponse(request, "campanhas.html",
                                      {"usuario": u, "campanhas": campanhas})


@app.get("/campanhas/{cid:int}", response_class=HTMLResponse)
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


@app.post("/campanhas/{cid:int}/acao")
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


@app.get("/campanhas/{cid:int}/relatorio")
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


@app.get("/campanhas/{cid:int}/exportar-ura")
def baixar_exportacao_ura(request: Request, cid: int, arquivo: str = "relatorio", todos: int = 0):
    u = usuario_atual(request, "campanhas")
    try:
        pipeline, relatorio = exportar_ura.exportar(cid, bool(todos))
    except SystemExit as e:
        raise HTTPException(status_code=400, detail=str(e))
    alvo = pipeline if arquivo == "pipeline" else relatorio
    auditar(u["id"], "exportacao_ura", request, campanha=cid, arquivo=arquivo)
    return FileResponse(alvo, media_type="text/csv", filename=alvo.name)


# ------------------------------ pausa do operador ------------------------------
def evento_discadora(nome, canal):
    """Avisa a discadora na hora, por um evento de usuario da ARI."""
    httpx.post(f"{ARI}/events/user/{nome}", params={"application": "discadora"},
               json={"variables": {"canal": canal}}, auth=ARI_AUTH, timeout=5)


def canal_do_operador(u):
    with db() as conn:
        op = conn.execute("SELECT canal FROM operadores_online WHERE ramal = %s",
                          (u["ramal"],)).fetchone()
    return op["canal"] if op else None


@app.post("/operador/pausar")
def pausar(request: Request):
    u = operador_com_ramal(request)
    canal = canal_do_operador(u)
    if not canal:
        return JSONResponse({"ok": False, "erro": "Você não está na fila"}, status_code=409)
    evento_discadora("pausar", canal)
    auditar(u["id"], "operador_pausa", request)
    return {"ok": True}


@app.post("/operador/retomar")
def retomar(request: Request):
    u = operador_com_ramal(request)
    canal = canal_do_operador(u)
    if not canal:
        return JSONResponse({"ok": False, "erro": "Você não está na fila"}, status_code=409)
    evento_discadora("retomar", canal)
    auditar(u["id"], "operador_retomar", request)
    return {"ok": True}


# ------------------------------ nova campanha: base propria ------------------------------
import time as _time
import campanhas as mod_campanhas

_regioes = {"quando": 0.0, "dados": []}


def regioes_base(forcar=False):
    """Contatos na base por UF, mesorregiao e cidade (guardado por 10 minutos)."""
    if forcar or not _regioes["dados"] or _time.time() - _regioes["quando"] > 600:
        with psycopg.connect(mod_campanhas.DSN_MAILING, row_factory=dict_row) as m:
            _regioes["dados"] = m.execute("""
                SELECT uf, coalesce(mesorregiao, '') AS mesorregiao, cidade, segmento, total, celulares
                FROM vw_discadora_saldo ORDER BY uf, mesorregiao, cidade""").fetchall()
        _regioes["quando"] = _time.time()
    return _regioes["dados"]


@app.get("/campanhas/nova", response_class=HTMLResponse)
def nova_campanha(request: Request):
    u = usuario_atual(request, "campanhas")
    return templates.TemplateResponse(request, "nova_campanha.html", {"usuario": u})


@app.get("/campanhas/nova/base", response_class=HTMLResponse)
def nova_base(request: Request, atualizar: int = 0):
    u = usuario_atual(request, "campanhas")
    return templates.TemplateResponse(request, "nova_base.html",
                                      {"usuario": u, "regioes": regioes_base(bool(atualizar)), "atual": {}})


@app.post("/campanhas/nova/base")
async def criar_campanha_base(request: Request):
    u = usuario_atual(request, "campanhas")
    f = await request.form()
    tipo = f.get("tipo", "call")
    tentativas = int(f.get("tentativas") or 1)
    if tipo not in ("call", "ura") or tentativas not in (1, 2, 3):
        raise HTTPException(status_code=400)
    filtro = filtro_do_formulario(f)
    dias = dias_do_formulario(f)
    quarentena = int(f["quarentena"]) if f.get("quarentena") else None
    lote = max(100, int(f.get("lote") or 5000))

    def criar():
        with psycopg.connect(mod_campanhas.DSN_DISCADORA) as conn:
            cid = mod_campanhas.criar_campanha(conn, f["projeto"].strip(), f["nome"].strip(),
                                               filtro, quarentena, tentativas)
            conn.execute("""UPDATE campanhas
                            SET tipo = %s, janela_inicio = %s, janela_fim = %s, max_canais = %s,
                                dias_semana = %s, data_inicio = %s, data_fim = %s
                            WHERE id = %s""",
                         (tipo, f.get("janela_inicio") or "09:00", f.get("janela_fim") or "20:00",
                          int(f["max_canais"]) if tipo == "ura" and f.get("max_canais") else None,
                          dias, f.get("data_inicio") or None, f.get("data_fim") or None, cid))
            conn.commit()
            _, _, inseridos = mod_campanhas.sortear_lote(conn, cid, lote)
        return cid, inseridos

    # o sorteio leva alguns segundos: roda fora do laco principal para nao travar o painel
    cid, inseridos = await run_in_threadpool(criar)
    auditar(u["id"], "campanha_criada", request, campanha=cid, origem="base",
            filtro=filtro, contatos=inseridos)
    return RedirectResponse(f"/campanhas/{cid}", status_code=303)


# ------------------------------ ligacoes em tempo real ------------------------------
ROTULOS = {"atendida": "Atendida", "respondente": "Respondente", "caixa_postal": "Caixa postal",
           "nao_atende": "Não atendeu", "recusou": "Rejeitada", "ocupado": "Ocupado",
           "indisponivel": "Desligado/fora de área", "invalido": "Número inválido",
           "falha_rede": "Falha de rede", "erro_originacao": "Erro técnico",
           "abandono": "Abandono", "operador_desligou": "Operador desligou", "outro": "Outro"}


@app.get("/campanhas/{cid:int}/tentativas")
def tentativas_recentes(request: Request, cid: int):
    usuario_atual(request, "campanhas")
    with db() as conn:
        linhas = conn.execute("""
            SELECT t.inicio, t.telefone, coalesce(t.operador_login, t.operador) AS operador,
                   t.resultado, t.atendida_em IS NOT NULL AS atendida, t.fim IS NULL AS andamento,
                   t.conversa_s, t.sip_codigo, t.tabulacao
            FROM tentativas t WHERE t.campanha_id = %s
            ORDER BY t.id DESC LIMIT 50""", (cid,)).fetchall()
    saida = []
    for t in linhas:
        if t["andamento"]:
            situacao, classe = ("Em conversa" if t["atendida"] else "Discando"), "andamento"
        else:
            situacao = ROTULOS.get(t["resultado"], t["resultado"] or "—")
            classe = "ok" if t["resultado"] in ("atendida", "respondente") else "nao"
        saida.append({"hora": t["inicio"].strftime("%H:%M:%S"), "telefone": t["telefone"],
                      "operador": t["operador"], "situacao": situacao, "classe": classe,
                      "conversa": t["conversa_s"], "sip": t["sip_codigo"],
                      "tabulacao": t["tabulacao"]})
    return saida


# ------------------------------ configuracoes da campanha ------------------------------
import re as _re, unicodedata as _ud
from starlette.concurrency import run_in_threadpool

DIAS = [(1, "Seg"), (2, "Ter"), (3, "Qua"), (4, "Qui"), (5, "Sex"), (6, "Sáb"), (7, "Dom")]
PERFIS_VALIDOS = {"misto", "misto_sem_quentes", "quentes", "respondentes", "atendentes", "impuros"}
CAMPOS_BASE = [("cidade", "Cidade"), ("uf", "UF"), ("bairro", "Bairro"),
               ("mesorregiao", "Mesorregião"), ("regional", "Regional"),
               ("tipo", "Tipo de telefone"), ("sexo", "Sexo"), ("idade", "Faixa etária"),
               ("escolaridade", "Escolaridade"), ("renda", "Renda"), ("ocupacao", "Ocupação")]


def filtro_do_formulario(f):
    filtro = {"uf": (f.get("uf") or "").upper()}
    for chave in ("mesorregiao", "cidade"):
        if f.get(chave):
            filtro[chave] = f[chave]
    if f.get("tipo_tel") in ("CELULAR", "FIXO"):
        filtro["tipo"] = f["tipo_tel"]
    if f.get("perfil") in PERFIS_VALIDOS and f["perfil"] != "misto":
        filtro["perfil"] = f["perfil"]
    return filtro


def dias_do_formulario(f):
    dias = {int(d) for d in f.getlist("dias") if d.isdigit() and 1 <= int(d) <= 7}
    return sorted(dias) or list(range(1, 8))


def codigo_de(nome):
    t = _ud.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    return _re.sub(r"[^a-z0-9]+", "_", t).strip("_") or "opcao"


def campos_disponiveis(conn, c):
    if c["origem"] == "lista" and c["lista_id"]:
        cols = conn.execute("SELECT colunas FROM listas WHERE id = %s",
                            (c["lista_id"],)).fetchone()["colunas"] or []
        return [(col, col) for col in cols]
    # campos padrao + qualquer outro campo presente nos contatos desta campanha
    extras = [r["k"] for r in conn.execute("""
        SELECT DISTINCT jsonb_object_keys(dados) AS k
        FROM (SELECT dados FROM campanha_contatos WHERE campanha_id = %s LIMIT 200) s""",
        (c["id"],)).fetchall()]
    conhecidos = {k for k, _ in CAMPOS_BASE}
    return CAMPOS_BASE + [(k, k) for k in sorted(extras) if k not in conhecidos]


@app.get("/campanhas/{cid:int}/configurar", response_class=HTMLResponse)
def configurar(request: Request, cid: int):
    u = usuario_atual(request, "campanhas")
    with db() as conn:
        c = buscar_campanha(conn, cid)
        disponiveis = campos_disponiveis(conn, c)
    escolhidos = {x["campo"]: x.get("rotulo") or x["campo"] for x in c["campos_tela"]}
    tab_texto = "\n".join(o["nome"] + (" *" if o.get("bloqueia") else "") for o in c["tabulacoes"])
    tem_segmento = c["origem"] == "base" and "uf" in (c["filtro"] or {})
    return templates.TemplateResponse(request, "configurar.html", {
        "usuario": u, "c": c, "dias": DIAS, "disponiveis": disponiveis,
        "escolhidos": escolhidos, "tab_texto": tab_texto, "tem_segmento": tem_segmento,
        "regioes": regioes_base() if tem_segmento else [], "atual": c["filtro"] or {}})


@app.post("/campanhas/{cid:int}/configurar")
async def salvar_configuracao(request: Request, cid: int):
    u = usuario_atual(request, "campanhas")
    f = await request.form()

    def salvar():
        with db() as conn:
            c = buscar_campanha(conn, cid)
            conn.execute("""UPDATE campanhas
                            SET nome = %s, dias_semana = %s, data_inicio = %s, data_fim = %s,
                                janela_inicio = %s, janela_fim = %s, max_tentativas = %s,
                                quarentena_dias = %s, max_canais = %s
                            WHERE id = %s""",
                         ((f.get("nome") or c["nome"]).strip(), dias_do_formulario(f),
                          f.get("data_inicio") or None, f.get("data_fim") or None,
                          f.get("janela_inicio") or c["janela_inicio"],
                          f.get("janela_fim") or c["janela_fim"],
                          int(f.get("tentativas") or c["max_tentativas"]),
                          int(f["quarentena"]) if f.get("quarentena") else None,
                          int(f["max_canais"]) if c["tipo"] == "ura" and f.get("max_canais") else c["max_canais"],
                          cid))
            trocados = None
            if c["origem"] == "base" and "uf" in (c["filtro"] or {}) and f.get("uf"):
                conn.execute("UPDATE campanhas SET filtro = %s WHERE id = %s",
                             (Jsonb(filtro_do_formulario(f)), cid))
                if f.get("trocar_pendentes"):
                    trocados = conn.execute("""
                        DELETE FROM campanha_contatos cc
                        WHERE cc.campanha_id = %s AND cc.status = 'pendente' AND cc.tentativas = 0
                          AND NOT EXISTS (SELECT 1 FROM tentativas t
                                          WHERE t.campanha_contato_id = cc.id)""", (cid,)).rowcount
            if c["tipo"] == "call":
                disponiveis = campos_disponiveis(conn, c)
                marcados = set(f.getlist("campos"))
                campos = [{"campo": k, "rotulo": (f.get(f"rotulo_{k}") or k).strip()}
                          for k, _ in disponiveis if k in marcados]
                antigos = {o["nome"]: o["codigo"] for o in c["tabulacoes"]}
                opcoes = []
                for linha in (f.get("tabulacoes") or "").splitlines():
                    nome = linha.strip()
                    if not nome:
                        continue
                    bloqueia = nome.endswith("*")
                    nome = nome.rstrip("*").strip()
                    opcao = {"codigo": antigos.get(nome) or codigo_de(nome), "nome": nome}
                    if bloqueia:
                        opcao["bloqueia"] = True
                    opcoes.append(opcao)
                conn.execute("""UPDATE campanhas SET campos_tela = %s, questionario_url = %s,
                                                     tabulacao_ativa = %s, tabulacoes = %s
                                WHERE id = %s""",
                             (Jsonb(campos), (f.get("questionario_url") or "").strip() or None,
                              bool(f.get("tabulacao_ativa")), Jsonb(opcoes), cid))
        sorteados = None
        if trocados is not None:
            with psycopg.connect(mod_campanhas.DSN_DISCADORA) as conn2:
                _, _, sorteados = mod_campanhas.sortear_lote(conn2, cid,
                                                             max(100, int(f.get("lote") or 5000)))
        return trocados, sorteados

    trocados, sorteados = await run_in_threadpool(salvar)
    auditar(u["id"], "campanha_configurada", request, campanha=cid,
            trocados=trocados, novos=sorteados)
    return RedirectResponse(f"/campanhas/{cid}", status_code=303)


# ------------------------------ nova campanha: lista de cliente ------------------------------
import uuid
from fastapi import UploadFile, File
import importar_lista, campanha_lista

PASTA_UPLOADS = Path.home() / "lab" / "uploads"
PASTA_UPLOADS.mkdir(exist_ok=True)


def tela_lista(request, u, etapa, **contexto):
    return templates.TemplateResponse(request, "nova_lista.html",
                                      {"usuario": u, "etapa": etapa, "dias": DIAS, **contexto})


@app.get("/campanhas/nova/lista", response_class=HTMLResponse)
def nova_lista(request: Request):
    u = usuario_atual(request, "campanhas")
    return tela_lista(request, u, "arquivo")


@app.post("/campanhas/nova/lista/arquivo", response_class=HTMLResponse)
async def nova_lista_arquivo(request: Request, arquivo: UploadFile = File(...),
                             cliente: str = Form(...), projeto: str = Form(...)):
    u = usuario_atual(request, "campanhas")
    if not (arquivo.filename or "").lower().endswith(".csv"):
        return tela_lista(request, u, "erro", erro="O arquivo precisa ser .csv.")
    token = uuid.uuid4().hex
    destino = PASTA_UPLOADS / f"{token}.csv"
    destino.write_bytes(await arquivo.read())
    try:
        colunas, linhas, codificacao, separador = importar_lista.ler_csv(destino)
    except Exception as e:
        destino.unlink(missing_ok=True)
        return tela_lista(request, u, "erro", erro=f"Não consegui ler o arquivo: {e}")
    # sugestoes automaticas pelo nome das colunas
    sug_tel = [c for c in colunas if re.search(r"tel|fone|cel|phone", c, re.I)]
    sug_uf = next((c for c in colunas if c.strip().lower() == "uf"), "")
    sug_cidade = next((c for c in colunas
                       if re.search(r"cidade|municipio|munic", c, re.I)), "")
    return tela_lista(request, u, "colunas", token=token, nome_arquivo=arquivo.filename,
                      cliente=cliente, projeto=projeto, colunas=colunas, amostra=linhas[:5],
                      total=len(linhas), codificacao=codificacao, separador=separador,
                      sug_tel=sug_tel, sug_uf=sug_uf, sug_cidade=sug_cidade)


@app.post("/campanhas/nova/lista/importar", response_class=HTMLResponse)
async def nova_lista_importar(request: Request):
    u = usuario_atual(request, "campanhas")
    f = await request.form()
    token = f.get("token", "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise HTTPException(status_code=400)
    caminho = PASTA_UPLOADS / f"{token}.csv"
    if not caminho.exists():
        return tela_lista(request, u, "erro", erro="O arquivo expirou. Envie de novo.")
    telefones = f.getlist("telefones")
    if not telefones:
        return tela_lista(request, u, "erro", erro="Marque pelo menos uma coluna de telefone.")

    def rodar():
        return importar_lista.importar(caminho, f["cliente"].strip(), f["projeto"].strip(),
                                       telefones, nome=Path(f["nome_arquivo"]).stem,
                                       uf=f.get("uf") or None, cidade=f.get("cidade") or None)
    try:
        resumo = await run_in_threadpool(rodar)
    except SystemExit as e:
        return tela_lista(request, u, "erro", erro=str(e))
    caminho.unlink(missing_ok=True)          # os dados ja estao no banco: o arquivo sai do disco
    auditar(u["id"], "lista_importada", request, lista=resumo["lista_id"],
            linhas=resumo["linhas"], validos=resumo["validos"])
    return tela_lista(request, u, "campanha", resumo=resumo, nome_arquivo=f["nome_arquivo"])


@app.post("/campanhas/nova/lista/criar")
async def nova_lista_criar(request: Request):
    u = usuario_atual(request, "campanhas")
    f = await request.form()
    lista_id = int(f["lista_id"])
    tipo = f.get("tipo", "call")
    tentativas = int(f.get("tentativas") or 1)
    if tipo not in ("call", "ura") or tentativas not in (1, 2, 3):
        raise HTTPException(status_code=400)
    quarentena = int(f["quarentena"]) if f.get("quarentena") else None

    def criar():
        r = campanha_lista.criar(lista_id, f["nome"].strip(), quarentena, tentativas)
        with db() as conn:
            conn.execute("""UPDATE campanhas
                            SET tipo = %s, janela_inicio = %s, janela_fim = %s, max_canais = %s,
                                dias_semana = %s, data_inicio = %s, data_fim = %s
                            WHERE id = %s""",
                         (tipo, f.get("janela_inicio") or "09:00", f.get("janela_fim") or "20:00",
                          int(f["max_canais"]) if tipo == "ura" and f.get("max_canais") else None,
                          dias_do_formulario(f), f.get("data_inicio") or None,
                          f.get("data_fim") or None, r["campanha_id"]))
        return r

    r = await run_in_threadpool(criar)
    auditar(u["id"], "campanha_criada", request, campanha=r["campanha_id"], origem="lista",
            lista=lista_id, na_fila=r["na_fila"], em_quarentena=r["em_quarentena"])
    return RedirectResponse(f"/campanhas/{r['campanha_id']}", status_code=303)


# ------------------------------ roteiro da URA ------------------------------
PASTA_SONS = Path("/var/lib/asterisk/sounds")
AUDIOS_GERAIS = [("ura_abertura", "Abertura"), ("ura_invalida", "Opção inválida"),
                 ("ura_encerramento", "Encerramento")]


def converter_audio(cid, conteudo, nome_original):
    """Converte WAV/MP3 para o formato de telefone e devolve o caminho usado pelo Asterisk."""
    ext = Path(nome_original or "").suffix.lower()
    if ext not in (".wav", ".mp3"):
        raise ValueError("Envie o áudio em WAV ou MP3.")
    pasta = PASTA_SONS / "ura" / f"c{cid}"
    pasta.mkdir(parents=True, exist_ok=True)
    nome = uuid.uuid4().hex[:12]
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(conteudo)
        origem = tmp.name
    destino = pasta / f"{nome}.wav"
    r = subprocess.run(["sox", origem, "-r", "8000", "-c", "1", "-b", "16", str(destino)],
                       capture_output=True, text=True)
    os.unlink(origem)
    if r.returncode != 0:
        raise ValueError("Não consegui converter o áudio: " + r.stderr.strip()[:200])
    destino.chmod(0o664)
    return f"ura/c{cid}/{nome}"


def apagar_audio(caminho, cid):
    """Apaga um audio antigo, mas so os da pasta desta campanha."""
    if caminho and caminho.startswith(f"ura/c{cid}/"):
        (PASTA_SONS / f"{caminho}.wav").unlink(missing_ok=True)


@app.get("/campanhas/{cid:int}/roteiro", response_class=HTMLResponse)
def roteiro(request: Request, cid: int, erro: str = "", ok: str = ""):
    u = usuario_atual(request, "campanhas")
    with db() as conn:
        c = buscar_campanha(conn, cid)
        if c["tipo"] != "ura":
            raise HTTPException(status_code=400, detail="Só campanhas de URA têm roteiro")
        perguntas = conn.execute("""SELECT ordem, audio, teclas_validas, timeout_s
                                    FROM ura_perguntas WHERE campanha_id = %s ORDER BY ordem""",
                                 (cid,)).fetchall()
    motor = systemctl("is-active", servico(c)).stdout.strip()
    return templates.TemplateResponse(request, "roteiro.html",
        {"usuario": u, "c": c, "perguntas": perguntas, "gerais": AUDIOS_GERAIS,
         "motor": motor, "erro": erro, "ok": ok})


@app.post("/campanhas/{cid:int}/roteiro")
async def salvar_roteiro(request: Request, cid: int):
    u = usuario_atual(request, "campanhas")
    f = await request.form()
    acao = f.get("acao")
    arquivo = f.get("arquivo")
    conteudo = await arquivo.read() if getattr(arquivo, "filename", "") else None

    def teclas_validas(v):
        v = (v or "").strip()
        if not re.fullmatch(r"[0-9*#]+", v):
            raise ValueError("Teclas válidas: use só dígitos, * e #, por exemplo 123.")
        return v

    def tempo_espera(v):
        n = int(v or 7)
        if not 3 <= n <= 30:
            raise ValueError("O tempo de espera deve ficar entre 3 e 30 segundos.")
        return n

    def executar():
        with db() as conn:
            c = buscar_campanha(conn, cid)
            if c["tipo"] != "ura":
                raise ValueError("Só campanhas de URA têm roteiro.")
            if acao == "geral":
                coluna = f.get("coluna")
                if coluna not in dict(AUDIOS_GERAIS) or not conteudo:
                    raise ValueError("Escolha um arquivo de áudio.")
                novo = converter_audio(cid, conteudo, arquivo.filename)
                apagar_audio(c[coluna], cid)
                # a coluna vem de uma lista fixa (AUDIOS_GERAIS), nunca do texto do formulario
                conn.execute(f"UPDATE campanhas SET {coluna} = %s WHERE id = %s", (novo, cid))
                return "Áudio atualizado."
            if acao == "nova":
                n = conn.execute("SELECT count(*) AS n FROM ura_perguntas WHERE campanha_id = %s",
                                 (cid,)).fetchone()["n"]
                if n >= 12:
                    raise ValueError("O limite é de 12 perguntas.")
                if not conteudo:
                    raise ValueError("Envie o áudio da pergunta.")
                teclas, espera = teclas_validas(f.get("teclas")), tempo_espera(f.get("timeout"))
                novo = converter_audio(cid, conteudo, arquivo.filename)
                conn.execute("""INSERT INTO ura_perguntas
                                  (campanha_id, ordem, audio, teclas_validas, timeout_s)
                                VALUES (%s, %s, %s, %s, %s)""", (cid, n + 1, novo, teclas, espera))
                return f"Pergunta {n + 1} adicionada."
            if acao == "editar":
                ordem = int(f["ordem"])
                p = conn.execute("""SELECT audio FROM ura_perguntas
                                    WHERE campanha_id = %s AND ordem = %s""", (cid, ordem)).fetchone()
                if not p:
                    raise ValueError("Pergunta não encontrada.")
                teclas, espera = teclas_validas(f.get("teclas")), tempo_espera(f.get("timeout"))
                audio = p["audio"]
                if conteudo:
                    audio = converter_audio(cid, conteudo, arquivo.filename)
                    apagar_audio(p["audio"], cid)
                conn.execute("""UPDATE ura_perguntas SET audio = %s, teclas_validas = %s, timeout_s = %s
                                WHERE campanha_id = %s AND ordem = %s""",
                             (audio, teclas, espera, cid, ordem))
                return f"Pergunta {ordem} salva."
            if acao == "remover":
                p = conn.execute("""SELECT ordem, audio FROM ura_perguntas WHERE campanha_id = %s
                                    ORDER BY ordem DESC LIMIT 1""", (cid,)).fetchone()
                if not p:
                    raise ValueError("Não há perguntas para remover.")
                conn.execute("DELETE FROM ura_perguntas WHERE campanha_id = %s AND ordem = %s",
                             (cid, p["ordem"]))
                apagar_audio(p["audio"], cid)
                return f"Pergunta {p['ordem']} removida."
            raise ValueError("Ação desconhecida.")

    try:
        msg = await run_in_threadpool(executar)
    except ValueError as e:
        return RedirectResponse(f"/campanhas/{cid}/roteiro?" + urlencode({"erro": str(e)}),
                                status_code=303)
    auditar(u["id"], "roteiro_ura", request, campanha=cid, operacao=acao)
    return RedirectResponse(f"/campanhas/{cid}/roteiro?" + urlencode({"ok": msg}), status_code=303)


@app.get("/campanhas/{cid:int}/roteiro/audio")
def ouvir_audio_roteiro(request: Request, cid: int, caminho: str):
    usuario_atual(request, "campanhas")
    if not re.fullmatch(r"ura/[A-Za-z0-9_/-]+", caminho) or ".." in caminho:
        raise HTTPException(status_code=400)
    arquivo = PASTA_SONS / f"{caminho}.wav"
    if not arquivo.exists():
        raise HTTPException(status_code=404)
    return FileResponse(arquivo, media_type="audio/wav")


# ------------------------------ acompanhamento ------------------------------
@app.get("/acompanhamento", response_class=HTMLResponse)
def acompanhamento(request: Request):
    u = usuario_atual(request, "acompanhamento")
    return templates.TemplateResponse(request, "acompanhamento.html", {"usuario": u})


def _tempo(estado=None):
    """Soma, em segundos, o tempo de hoje num estado (ou em todos)."""
    filtro = f" FILTER (WHERE estado = '{estado}')" if estado else ""
    return (f"coalesce(sum(extract(epoch FROM coalesce(fim, now()) - "
            f"greatest(inicio, current_date::timestamptz))){filtro}, 0)::int")


@app.get("/acompanhamento/dados")
def acompanhamento_dados(request: Request):
    usuario_atual(request, "acompanhamento")
    with db() as conn:
        agora_ops = conn.execute("""
            SELECT o.login, o.ramal, o.estado, o.pausa_solicitada,
                   extract(epoch FROM now() - o.estado_desde)::int AS segundos,
                   c.nome AS campanha
            FROM operadores_online o LEFT JOIN campanhas c ON c.id = o.campanha_id""").fetchall()
        tempos = {r["login"]: r for r in conn.execute(f"""
            SELECT login, max(ramal) AS ramal,
                   {_tempo()} AS total, {_tempo('pausa')} AS pausa,
                   {_tempo('em_chamada')} AS em_chamada, {_tempo('tabulando')} AS tabulando,
                   {_tempo('livre')} AS livre
            FROM operador_estados
            WHERE coalesce(fim, now()) >= current_date
            GROUP BY login""").fetchall()}
        atend = {r["login"]: r for r in conn.execute("""
            SELECT operador_login AS login,
                   count(*) FILTER (WHERE atendida_em IS NOT NULL) AS atendidas,
                   round(avg(conversa_s) FILTER (WHERE atendida_em IS NOT NULL))::int AS tma
            FROM tentativas
            WHERE inicio >= current_date AND operador_login IS NOT NULL
            GROUP BY 1""").fetchall()}
        totais = conn.execute("""
            SELECT count(*) AS tentativas,
                   count(*) FILTER (WHERE atendida_em IS NOT NULL) AS atendidas,
                   count(*) FILTER (WHERE fim IS NULL) AS em_andamento,
                   count(*) FILTER (WHERE fim IS NOT NULL) AS finalizadas,
                   count(*) FILTER (WHERE fim IS NOT NULL
                                    AND fim - inicio <= interval '6 seconds') AS curtas
            FROM tentativas WHERE inicio >= current_date""").fetchone()
        limite = int(conn.execute("""SELECT valor FROM config
                                     WHERE chave = 'limite_diario_empresa'""").fetchone()["valor"])
        por_hora = conn.execute("""
            SELECT extract(hour FROM inicio)::int AS hora, count(*) AS tentativas,
                   count(*) FILTER (WHERE atendida_em IS NOT NULL) AS atendidas
            FROM tentativas WHERE inicio >= current_date
            GROUP BY 1 ORDER BY 1""").fetchall()
        troncos = conn.execute("""
            SELECT t.nome, t.ativo, t.canais, t.limite_diario,
                   (SELECT count(*) FROM canais_em_uso c WHERE c.tronco_id = t.id) AS em_uso,
                   (SELECT count(*) FROM tentativas x
                    WHERE x.tronco_id = t.id AND x.inicio >= current_date) AS hoje
            FROM troncos t ORDER BY t.prioridade, t.id""").fetchall()
        campanhas_ativas = conn.execute("""
            SELECT c.id, c.nome, c.tipo, c.status,
                   count(cc.id) FILTER (WHERE cc.status = 'pendente') AS pendentes,
                   (SELECT count(*) FROM tentativas t
                    WHERE t.campanha_id = c.id AND t.inicio >= current_date) AS hoje
            FROM campanhas c LEFT JOIN campanha_contatos cc ON cc.campanha_id = c.id
            WHERE c.status IN ('ativa', 'pausada')
            GROUP BY c.id ORDER BY c.id DESC""").fetchall()

    def linha(login, ramal, estado, segundos, campanha, pausa_agendada):
        tp, at = tempos.get(login) or {}, atend.get(login) or {}
        return {"login": login or "(sem login)", "ramal": ramal, "estado": estado,
                "segundos": segundos, "campanha": campanha, "pausa_agendada": pausa_agendada,
                "logado": tp.get("total", 0),
                "em_chamada": tp.get("em_chamada", 0) + tp.get("tabulando", 0),
                "pausa": tp.get("pausa", 0), "disponivel": tp.get("livre", 0),
                "atendidas": at.get("atendidas", 0), "tma": at.get("tma")}

    operadores = [linha(o["login"], o["ramal"], o["estado"], o["segundos"], o["campanha"],
                        o["pausa_solicitada"]) for o in agora_ops]
    online = {o["login"] for o in agora_ops}
    for login, tp in tempos.items():             # trabalharam hoje, mas sairam da fila
        if login not in online:
            operadores.append(linha(login, tp["ramal"], "fora", None, None, False))
    operadores.sort(key=lambda o: (len(o["login"]), o["login"]))
    for c in campanhas_ativas:
        c["motor"] = systemctl("is-active", servico(c)).stdout.strip()
    return {"operadores": operadores, "totais": totais, "limite": limite,
            "por_hora": por_hora, "troncos": troncos, "campanhas": campanhas_ativas,
            "atualizado": datetime.now().strftime("%H:%M:%S")}


# modulos ainda nao construidos: so verifica a permissao e mostra "em construcao"
@app.get("/{modulo}", response_class=HTMLResponse)
def em_construcao(request: Request, modulo: str):
    if modulo not in PERMISSOES["admin"]:
        raise HTTPException(status_code=404)
    u = usuario_atual(request, modulo)
    titulo = next(t for k, t, _ in MODULOS if k == modulo)
    return templates.TemplateResponse(request, "em_construcao.html",
                                      {"usuario": u, "titulo": titulo})


# ordem do menu: Acompanhamento primeiro, os demais na ordem original
MODULOS = sorted(MODULOS, key=lambda m: m[0] != "acompanhamento")
