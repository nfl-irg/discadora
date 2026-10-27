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


# modulos ainda nao construidos: so verifica a permissao e mostra "em construcao"
@app.get("/{modulo}", response_class=HTMLResponse)
def em_construcao(request: Request, modulo: str):
    if modulo not in PERMISSOES["admin"]:
        raise HTTPException(status_code=404)
    u = usuario_atual(request, modulo)
    titulo = next(t for k, t, _ in MODULOS if k == modulo)
    return templates.TemplateResponse(request, "em_construcao.html",
                                      {"usuario": u, "titulo": titulo})
