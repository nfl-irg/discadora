"""Discadora progressiva v3: fila do banco, rediscagem e reserva de canais por tronco."""
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


async def discar_para_livres():
    global ultimo_aviso_sem_canal
    if not pode_discar():
        return
    for op_id, op in list(operadores.items()):
        if not op["livre"]:
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

        # 2) pega o proximo contato da fila
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
                              (args.campanha, contato["id"], numero, cli_id, op["ramal"], op["login"], tronco_id))
            tentativa_id = (await cur.fetchone())[0]
        chamadas[cli_id] = {"contato": contato, "tentativa_id": tentativa_id,
                            "numero": numero, "operador": op_id, "ponte": None}
        print(f"{agora()}  discando {numero} pelo tronco {nome_tronco} "
              f"(ja teve {contato['tentativas']} tentativa(s)) para o operador {op['ramal']}")
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
    await db.execute("UPDATE operadores_online SET estado = 'livre', tentativa_atual = NULL WHERE canal = %s", (c["operador"],))
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
        await cur.execute("SELECT count(*) AS n FROM tentativas WHERE campanha_contato_id = %s",
                          (contato["id"],))
        feitas = (await cur.fetchone())["n"]
        contadas = contato["tentativas"] + (1 if regra["conta_tentativa"] else 0)

        if regra["finaliza"]:
            status, motivo = "finalizado", resultado
        elif contadas >= campanha["max_tentativas"]:
            status, motivo = "finalizado", f"{resultado} (tentativas esgotadas)"
        elif feitas >= campanha["max_tentativas"] + LIMITE_ERROS_TECNICOS:
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
    pausa = PAUSA_POS_CHAMADA if c.get("atendida_em") else 0
    asyncio.create_task(liberar_operador(c["operador"], pausa))


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
            await db.execute("UPDATE operadores_online SET visto_em = now() WHERE canal = ANY(%s)", (list(operadores),))
        if operadores:            # sinal de vida dos operadores logados
            await db.execute("UPDATE operadores_online SET visto_em = now() WHERE canal = ANY(%s)", (list(operadores),))
        await discar_para_livres()
        if time.time() - ultimo_lote > 60:
            ultimo_lote = time.time()
            await repor_lote()


async def tratar(ev):
    tipo = ev["type"]
    if tipo in IGNORAR:
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
            operadores[cid] = {"nome": canal["name"], "ramal": ramal, "login": login, "livre": True}
            await db.execute("""INSERT INTO operadores_online (canal, ramal, campanha_id)
                                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                             (cid, ramal, args.campanha))
            print(f"{agora()}  operador {login or '(sem login)'} (ramal {ramal}) entrou e esta disponivel")
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
                print(f"{agora()}  {c['numero']} ATENDEU -> conectado ao operador")
                await db.execute("UPDATE operadores_online SET estado = 'em_chamada', tentativa_atual = %s WHERE canal = %s", (c["tentativa_id"], c["operador"]))

    elif tipo == "StasisEnd":
        if cid in chamadas:
            await finalizar(cid, chamadas[cid].get("resultado_forcado", "atendida"), None, None)
        elif cid in operadores:
            op = operadores.pop(cid)
            await db.execute("DELETE FROM operadores_online WHERE canal = %s", (cid,))
            print(f"{agora()}  operador {op['ramal']} saiu")
            for cli_id, c in list(chamadas.items()):
                if c["operador"] == cid and c.get("ponte"):
                    c["resultado_forcado"] = "operador_desligou"
                    await http.delete(f"{ARI}/channels/{cli_id}")

    elif tipo == "ChannelDestroyed" and cid in chamadas:
        c = chamadas[cid]
        await db.execute("UPDATE tentativas SET sip_codigo = %s WHERE id = %s", (ev.get("tech_cause"), c["tentativa_id"]))
        if c.get("atendida_em"):
            await finalizar(cid, c.get("resultado_forcado", "atendida"), None, None)
        else:
            await finalizar(cid, classificar(ev.get("cause")), ev.get("cause"),
                            ev.get("cause_txt"))

    await discar_para_livres()


async def main():
    global http, db, args
    p = argparse.ArgumentParser(description="Discadora progressiva v3")
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

    # recuperacao de queda: contatos presos, canais e operadores que ficaram no placar
    await db.execute("""UPDATE campanha_contatos SET status = 'pendente'
                        WHERE campanha_id = %s AND status = 'discando'""", (args.campanha,))
    await db.execute("DELETE FROM canais_em_uso WHERE tipo = 'call'")
    await db.execute("DELETE FROM operadores_online")
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
            print("Operador: no navegador (7001), ligue para 900.")
            asyncio.create_task(relogio())
            async for msg in ws:
                await tratar(json.loads(msg))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nEncerrado.")
