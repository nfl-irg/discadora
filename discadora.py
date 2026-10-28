"""Discadora progressiva v4: fila do banco, rediscagem, reserva de canais por tronco,
tabulacao e pausa do operador (inclusive pausa apos a ligacao)."""
import argparse, asyncio, json, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
import httpx, psycopg, websockets
from psycopg.rows import dict_row

import campanhas                      # para sortear novos lotes da base propria

ARI = "http://127.0.0.1:8088/ari"
USUARIO = "discadora"
SENHA = (Path.home() / "lab" / "ari_senha.txt").read_text().strip()
APP = "discadora"
BINA = "4130000000"
TEMPO_TOQUE = 25
PAUSA_POS_CHAMADA = 5
FILA_MINIMA = 50
TAMANHO_LOTE = 5000
LIMITE_ERROS_TECNICOS = 5
IGNORAR = {"ChannelVarset", "ChannelDialplan"}
MANTER = object()                     # marcador: nao mexer no contato mostrado na tela

operadores, chamadas, campanha = {}, {}, {}
http = db = args = None
ultimo_aviso_sem_canal = 0.0


def agora():
    return datetime.now().strftime("%H:%M:%S")


def classificar(causa):
    if causa == 17:               return "ocupado"
    if causa in (18, 19):         return "nao_atende"
    if causa == 21:               return "recusou"
    if causa == 20:               return "indisponivel"
    if causa in (1, 3, 28):       return "invalido"
    if causa in (34, 38, 41, 42): return "falha_rede"
    return "outro"


def nome_op(op):
    return op["login"] or op["ramal"]


async def carregar_campanha():
    global campanha
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM campanhas WHERE id = %s", (args.campanha,))
        campanha = await cur.fetchone()


def pode_discar():
    hora = datetime.now().time()
    return (campanha["status"] == "ativa"
            and campanha["janela_inicio"] <= hora <= campanha["janela_fim"])


async def proximo_contato():
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM pegar_contato(%s)", (args.campanha,))
        return await cur.fetchone()


async def liberar_canal(cli_id):
    await db.execute("DELETE FROM canais_em_uso WHERE canal = %s", (cli_id,))


async def mudar_estado(op_id, estado, tentativa=MANTER):
    """Muda o estado do operador no placar e grava o historico (base do acompanhamento)."""
    op = operadores.get(op_id)
    if not op:
        return
    if tentativa is MANTER:
        await db.execute("""UPDATE operadores_online SET estado = %s, estado_desde = now()
                            WHERE canal = %s""", (estado, op_id))
    else:
        await db.execute("""UPDATE operadores_online
                            SET estado = %s, estado_desde = now(), tentativa_atual = %s
                            WHERE canal = %s""", (estado, tentativa, op_id))
    if op.get("estado") != estado:
        await db.execute("UPDATE operador_estados SET fim = now() WHERE ramal = %s AND fim IS NULL",
                         (op["ramal"],))
        await db.execute("""INSERT INTO operador_estados (login, ramal, campanha_id, estado)
                            VALUES (%s, %s, %s, %s)""",
                         (op["login"], op["ramal"], args.campanha, estado))
        op["estado"] = estado


def em_ligacao(op_id):
    return any(c["operador"] == op_id for c in chamadas.values())


async def discar_para_livres():
    global ultimo_aviso_sem_canal
    if not pode_discar():
        return
    for op_id, op in list(operadores.items()):
        if not op["livre"] or op["pausado"]:
            continue
        op["livre"] = False
        cli_id = f"cli-{uuid.uuid4().hex[:12]}"

        # 1) pede um canal ao banco (troncos, travas diarias e limite da empresa)
        async with db.cursor() as cur:
            await cur.execute("SELECT reservar_canal(%s, 'call', %s)", (cli_id, args.campanha))
            tronco_id = (await cur.fetchone())[0]
        if tronco_id is None:
            op["livre"] = True
            if time.time() - ultimo_aviso_sem_canal > 30:
                ultimo_aviso_sem_canal = time.time()
                print(f"{agora()}  sem canal disponivel (troncos cheios, inativos ou limite diario)")
            return

        # 2) pega o proximo contato da fila (respeitando as rodadas)
        contato = await proximo_contato()
        if contato is None:
            await liberar_canal(cli_id)
            op["livre"] = True
            return

        numero = contato["telefone"]
        async with db.cursor() as cur:
            await cur.execute("SELECT nome, endpoint FROM troncos WHERE id = %s", (tronco_id,))
            nome_tronco, modelo = await cur.fetchone()
            await cur.execute("""INSERT INTO tentativas (campanha_id, campanha_contato_id, telefone,
                                                         canal, operador, operador_login, tronco_id)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                              (args.campanha, contato["id"], numero, cli_id, op["ramal"],
                               op["login"], tronco_id))
            tentativa_id = (await cur.fetchone())[0]
        chamadas[cli_id] = {"contato": contato, "tentativa_id": tentativa_id,
                            "numero": numero, "operador": op_id, "ponte": None}
        print(f"{agora()}  discando {numero} pelo tronco {nome_tronco} "
              f"(ja teve {contato['tentativas']} tentativa(s)) para {nome_op(op)}")
        r = await http.post(f"{ARI}/channels", params={
            "endpoint": modelo.replace("{numero}", numero), "app": APP, "appArgs": "cliente",
            "channelId": cli_id, "callerId": BINA, "timeout": TEMPO_TOQUE})
        if r.status_code >= 400:
            await finalizar(cli_id, "erro_originacao", None,
                            f"HTTP {r.status_code} {' '.join(r.text.split())[:60]}")
        else:
            asyncio.create_task(vigiar(cli_id))


async def finalizar(cli_id, resultado, causa_codigo, causa_texto):
    c = chamadas.pop(cli_id, None)
    if c is None:
        return
    await liberar_canal(cli_id)                   # devolve o canal ao placar
    if c.get("ponte"):
        await http.delete(f"{ARI}/bridges/{c['ponte']}")
    conversa = round(time.time() - c["atendida_em"]) if c.get("atendida_em") else None
    contato = c["contato"]

    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute("""UPDATE tentativas SET fim = now(), resultado = %s, causa_codigo = %s,
                               causa_texto = %s, conversa_s = %s WHERE id = %s""",
                          (resultado, causa_codigo, causa_texto, conversa, c["tentativa_id"]))
        await cur.execute("SELECT * FROM regras_rediscagem WHERE resultado = %s", (resultado,))
        regra = await cur.fetchone() or {"finaliza": False, "intervalo": timedelta(hours=1),
                                         "conta_tentativa": True}
        # teto de seguranca: conta so as falhas do nosso lado (que nao consomem tentativa)
        await cur.execute("""SELECT count(*) AS n FROM tentativas t
                             JOIN regras_rediscagem r ON r.resultado = t.resultado
                             WHERE t.campanha_contato_id = %s AND NOT r.conta_tentativa""",
                          (contato["id"],))
        erros = (await cur.fetchone())["n"]
        contadas = contato["tentativas"] + (1 if regra["conta_tentativa"] else 0)

        if regra["finaliza"]:
            status, motivo = "finalizado", resultado
        elif contadas >= campanha["max_tentativas"]:
            status, motivo = "finalizado", f"{resultado} (tentativas esgotadas)"
        elif erros >= LIMITE_ERROS_TECNICOS:
            status, motivo = "finalizado", "falha_tecnica"
        else:
            status, motivo = "pendente", resultado

        await cur.execute("""UPDATE campanha_contatos
                             SET status = %s, tentativas = %s, ultimo_resultado = %s,
                                 proxima_tentativa = now() + COALESCE(%s, interval '0'),
                                 atualizado_em = now()
                             WHERE id = %s""",
                          (status, contadas, motivo, regra["intervalo"], contato["id"]))
        if resultado == "atendida" and contato["lista_contato_id"]:
            await cur.execute("""UPDATE campanha_contatos
                                 SET status = 'finalizado', atualizado_em = now(),
                                     ultimo_resultado = 'atendido em outro telefone'
                                 WHERE campanha_id = %s AND lista_contato_id = %s
                                   AND id <> %s AND status = 'pendente'""",
                              (args.campanha, contato["lista_contato_id"], contato["id"]))

    quando = "" if status == "finalizado" else f" | nova tentativa em {regra['intervalo']}"
    print(f"{agora()}  >>> {c['numero']}: {resultado} "
          f"({causa_codigo or '-'} {causa_texto or ''}) -> {motivo} [{status}]{quando}")
    await depois_da_chamada(c)


async def depois_da_chamada(c):
    """Decide o que o operador faz agora: tabular, ficar em pausa ou receber a proxima."""
    op_id = c["operador"]
    op = operadores.get(op_id)
    if not op:
        return                                   # o operador ja saiu da fila
    atendida = bool(c.get("atendida_em"))
    precisa_tabular = False
    if atendida and campanha["tabulacao_ativa"]:
        async with db.cursor() as cur:
            await cur.execute("SELECT tabulacao FROM tentativas WHERE id = %s", (c["tentativa_id"],))
            precisa_tabular = (await cur.fetchone())[0] is None
    if precisa_tabular:
        await mudar_estado(op_id, "tabulando")
        print(f"{agora()}  aguardando a tabulacao de {nome_op(op)}")
    elif op["pausado"]:
        # pausa apos a ligacao: o contato continua na tela do operador
        await mudar_estado(op_id, "pausa", c["tentativa_id"] if atendida else None)
        print(f"{agora()}  {nome_op(op)} entrou em pausa")
    else:
        await mudar_estado(op_id, "livre", None)
        if campanha["tabulacao_ativa"]:
            pausa = 0
        else:
            pausa = PAUSA_POS_CHAMADA if atendida else 0
        asyncio.create_task(liberar_operador(op_id, pausa))


async def liberar_operador(op_id, pausa):
    await asyncio.sleep(pausa)
    op = operadores.get(op_id)
    if op:
        op["livre"] = True
    await discar_para_livres()


async def vigiar(cli_id):
    await asyncio.sleep(TEMPO_TOQUE + 15)
    c = chamadas.get(cli_id)
    if c and not c.get("atendida_em"):
        await http.delete(f"{ARI}/channels/{cli_id}")
        await finalizar(cli_id, "outro", None, "nenhum evento recebido")


async def repor_lote():
    filtro = campanha["filtro"] or {}
    if campanha["origem"] != "base" or "uf" not in filtro:
        return
    async with db.cursor() as cur:
        await cur.execute("""SELECT count(*) FROM campanha_contatos
                             WHERE campanha_id = %s AND status = 'pendente'""", (args.campanha,))
        pendentes = (await cur.fetchone())[0]
    if pendentes >= FILA_MINIMA:
        return
    print(f"{agora()}  fila com {pendentes} pendentes: sorteando novo lote...")

    def sortear():
        with psycopg.connect(campanhas.DSN_DISCADORA) as conn:
            return campanhas.sortear_lote(conn, args.campanha, TAMANHO_LOTE)

    lote_id, sorteados, inseridos = await asyncio.to_thread(sortear)
    print(f"{agora()}  lote {lote_id}: {inseridos} novos contatos na fila")


async def relogio():
    ultimo_lote = 0
    while True:
        await asyncio.sleep(5)
        await carregar_campanha()
        if operadores:            # sinal de vida dos operadores logados
            await db.execute("UPDATE operadores_online SET visto_em = now() WHERE canal = ANY(%s)",
                             (list(operadores),))
        await discar_para_livres()
        if time.time() - ultimo_lote > 60:
            ultimo_lote = time.time()
            await repor_lote()


async def evento_do_painel(ev):
    """Avisos enviados pelo painel: tabulacao, pausar e retomar."""
    nome = ev.get("eventname")
    op_id = (ev.get("userevent") or {}).get("canal")
    op = operadores.get(op_id)
    if not op:
        return
    if nome == "tabulacao" and op.get("estado") == "tabulando":
        if op["pausado"]:
            await mudar_estado(op_id, "pausa")           # mantem o contato na tela
            print(f"{agora()}  {nome_op(op)} tabulou e entrou em pausa")
        else:
            await mudar_estado(op_id, "livre", None)
            print(f"{agora()}  {nome_op(op)} tabulou e esta disponivel")
            asyncio.create_task(liberar_operador(op_id, 0))
    elif nome == "pausar":
        op["pausado"] = True
        await db.execute("UPDATE operadores_online SET pausa_solicitada = true WHERE canal = %s",
                         (op_id,))
        if op.get("estado") == "livre" and not em_ligacao(op_id):
            await mudar_estado(op_id, "pausa", None)     # sem chamada: pausa na hora
        print(f"{agora()}  {nome_op(op)} pediu pausa")
    elif nome == "retomar":
        op["pausado"] = False
        await db.execute("UPDATE operadores_online SET pausa_solicitada = false WHERE canal = %s",
                         (op_id,))
        if op.get("estado") == "pausa":
            await mudar_estado(op_id, "livre", None)
            asyncio.create_task(liberar_operador(op_id, 0))
        print(f"{agora()}  {nome_op(op)} voltou a ficar disponivel")


async def tratar(ev):
    tipo = ev["type"]
    if tipo in IGNORAR:
        return
    if tipo == "ChannelUserevent":
        await evento_do_painel(ev)
        return
    canal = ev.get("channel", {})
    cid = canal.get("id", "")

    if tipo == "StasisStart":
        papel = (ev.get("args") or [""])[0]
        if papel == "operador":
            await http.post(f"{ARI}/channels/{cid}/answer")
            ramal = canal["caller"]["number"]
            async with db.cursor() as cur:
                await cur.execute("""SELECT login FROM usuarios
                                     WHERE ramal = %s AND perfil = 'operador' AND ativo""", (ramal,))
                linha = await cur.fetchone()
            login = linha[0] if linha else None
            operadores[cid] = {"nome": canal["name"], "ramal": ramal, "login": login,
                               "livre": True, "pausado": False, "estado": None}
            await db.execute("""INSERT INTO operadores_online (canal, ramal, login, campanha_id)
                                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                             (cid, ramal, login, args.campanha))
            await mudar_estado(cid, "livre", None)
            print(f"{agora()}  operador {login or '(sem login)'} (ramal {ramal}) entrou na fila")
        elif papel == "cliente" and cid in chamadas:
            c = chamadas[cid]
            if c["operador"] not in operadores:
                c["resultado_forcado"] = "abandono"
                await http.delete(f"{ARI}/channels/{cid}")
            else:
                ponte = (await http.post(f"{ARI}/bridges",
                                         params={"type": "mixing"})).json()["id"]
                c.update(ponte=ponte, atendida_em=time.time())
                await http.post(f"{ARI}/bridges/{ponte}/addChannel",
                                params={"channel": f"{c['operador']},{cid}"})
                await http.post(f"{ARI}/bridges/{ponte}/record",
                                params={"name": cid, "format": "wav"})
                await db.execute("UPDATE tentativas SET atendida_em = now() WHERE id = %s",
                                 (c["tentativa_id"],))
                await mudar_estado(c["operador"], "em_chamada", c["tentativa_id"])
                print(f"{agora()}  {c['numero']} ATENDEU -> conectado a "
                      f"{nome_op(operadores[c['operador']])}")

    elif tipo == "StasisEnd":
        if cid in chamadas:
            await finalizar(cid, chamadas[cid].get("resultado_forcado", "atendida"), None, None)
        elif cid in operadores:
            op = operadores.pop(cid)
            await db.execute("DELETE FROM operadores_online WHERE canal = %s", (cid,))
            await db.execute("UPDATE operador_estados SET fim = now() WHERE ramal = %s AND fim IS NULL",
                             (op["ramal"],))
            print(f"{agora()}  operador {nome_op(op)} saiu da fila")
            for cli_id, c in list(chamadas.items()):
                if c["operador"] == cid and c.get("ponte"):
                    c["resultado_forcado"] = "operador_desligou"
                    await http.delete(f"{ARI}/channels/{cli_id}")

    elif tipo == "ChannelDestroyed" and cid in chamadas:
        c = chamadas[cid]
        await db.execute("UPDATE tentativas SET sip_codigo = %s WHERE id = %s",
                         (ev.get("tech_cause"), c["tentativa_id"]))
        if c.get("atendida_em"):
            await finalizar(cid, c.get("resultado_forcado", "atendida"), None, None)
        else:
            await finalizar(cid, classificar(ev.get("cause")), ev.get("cause"),
                            ev.get("cause_txt"))

    await discar_para_livres()


async def main():
    global http, db, args
    p = argparse.ArgumentParser(description="Discadora progressiva v4")
    p.add_argument("--campanha", type=int, required=True)
    args = p.parse_args()

    db = await psycopg.AsyncConnection.connect("dbname=discadora", autocommit=True)
    await carregar_campanha()
    if campanha is None:
        raise SystemExit(f"Campanha {args.campanha} nao existe")
    if campanha["tipo"] != "call":
        raise SystemExit(f"A campanha {args.campanha} e do tipo '{campanha['tipo']}', nao 'call'")

    # seguranca: telefones reais + so troncos de laboratorio = nao inicia
    async with db.cursor() as cur:
        await cur.execute("SELECT bool_and(position('@' in endpoint) = 0) FROM troncos WHERE ativo")
        so_lab = (await cur.fetchone())[0]
        await cur.execute("""SELECT bool_or(length(telefone) >= 10) FROM campanha_contatos
                             WHERE campanha_id = %s AND status <> 'finalizado'""", (args.campanha,))
        tem_reais = (await cur.fetchone())[0]
    if so_lab and tem_reais:
        raise SystemExit("A campanha tem telefones reais, mas so ha troncos de laboratorio ativos.")

    # recuperacao de queda: contatos presos, canais, operadores e estados abertos
    await db.execute("""UPDATE campanha_contatos SET status = 'pendente'
                        WHERE campanha_id = %s AND status = 'discando'""", (args.campanha,))
    await db.execute("DELETE FROM canais_em_uso WHERE tipo = 'call'")
    await db.execute("DELETE FROM operadores_online")
    await db.execute("UPDATE operador_estados SET fim = now() WHERE fim IS NULL")
    await db.execute("UPDATE campanhas SET status = 'ativa' WHERE id = %s AND status = 'rascunho'",
                     (args.campanha,))
    await carregar_campanha()

    url = f"ws://127.0.0.1:8088/ari/events?app={APP}&api_key={USUARIO}:{SENHA}"
    async with httpx.AsyncClient(auth=(USUARIO, SENHA), timeout=10) as cliente_http:
        http = cliente_http
        async with websockets.connect(url) as ws:
            print(f"Discadora no ar | campanha {campanha['id']} '{campanha['nome']}' | "
                  f"janela {campanha['janela_inicio']}-{campanha['janela_fim']} | "
                  f"max {campanha['max_tentativas']} tentativas")
            print("Operadores: abram a Tela do operador no painel e cliquem em Ficar disponivel.")
            asyncio.create_task(relogio())
            async for msg in ws:
                await tratar(json.loads(msg))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nEncerrado.")
