"""URA de pesquisa: disca, detecta secretaria, faz as perguntas e grava as teclas."""
import argparse, asyncio, json, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
import httpx, psycopg, websockets
from psycopg.rows import dict_row

ARI = "http://127.0.0.1:8088/ari"
USUARIO = "discadora"
SENHA = (Path.home() / "lab" / "ari_senha.txt").read_text().strip()
APP = "ura"
BINA = "4130000000"
TEMPO_TOQUE = 25
CHAMADAS_POR_SEGUNDO = 5        # ritmo maximo de novas ligacoes
MAX_REPETICOES = 1              # repeticoes por pergunta (silencio ou tecla invalida)
DURACAO_MAXIMA = 300            # seguranca: derruba chamadas com mais de 5 min
LIMITE_ERROS_TECNICOS = 5
DURACAO_MINIMA = 8              # lida da tabela config ao iniciar
IGNORAR = {"ChannelVarset", "ChannelDialplan", "ChannelStateChange", "ChannelConnectedLine",
           "Dial", "ChannelHangupRequest", "PlaybackStarted", "StasisEnd"}

chamadas, campanha, perguntas = {}, {}, []
http = db = args = None
ultimo_aviso = 0.0


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


async def liberar_canal(cid):
    await db.execute("DELETE FROM canais_em_uso WHERE canal = %s", (cid,))


# ------------------------------ discagem ------------------------------
async def discar():
    """Abre novas ligacoes ate o teto de canais, no maximo CHAMADAS_POR_SEGUNDO por vez."""
    global ultimo_aviso
    if not pode_discar():
        return
    teto = campanha["max_canais"] or 120
    novas = 0
    while len(chamadas) < teto and novas < CHAMADAS_POR_SEGUNDO:
        cid = f"ura-{uuid.uuid4().hex[:12]}"
        async with db.cursor() as cur:
            await cur.execute("SELECT reservar_canal(%s, 'ura', %s)", (cid, args.campanha))
            tronco_id = (await cur.fetchone())[0]
        if tronco_id is None:
            if time.time() - ultimo_aviso > 30:
                ultimo_aviso = time.time()
                print(f"{agora()}  sem canal para a URA (troncos cheios, reserva do call "
                      f"ou limite diario)")
            return
        contato = await proximo_contato()
        if contato is None:
            await liberar_canal(cid)
            return
        numero = contato["telefone"]
        async with db.cursor() as cur:
            await cur.execute("SELECT endpoint FROM troncos WHERE id = %s", (tronco_id,))
            modelo = (await cur.fetchone())[0]
            await cur.execute("""INSERT INTO tentativas (campanha_id, campanha_contato_id,
                                                         telefone, canal, tronco_id)
                                 VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                              (args.campanha, contato["id"], numero, cid, tronco_id))
            tentativa_id = (await cur.fetchone())[0]
        chamadas[cid] = {"contato": contato, "tentativa_id": tentativa_id, "numero": numero,
                         "estado": "tocando", "respostas": {}, "playback": None}
        print(f"{agora()}  discando {numero} (tronco {tronco_id})")
        r = await http.post(f"{ARI}/channels", params={
            "endpoint": modelo.replace("{numero}", numero), "app": APP, "appArgs": "atendeu",
            "channelId": cid, "callerId": BINA, "timeout": TEMPO_TOQUE})
        if r.status_code >= 400:
            await finalizar(cid, "erro_originacao", None,
                            f"HTTP {r.status_code} {' '.join(r.text.split())[:60]}")
        else:
            asyncio.create_task(vigiar(cid))
        novas += 1


async def vigiar(cid):
    await asyncio.sleep(TEMPO_TOQUE + 15)
    c = chamadas.get(cid)
    if c and not c.get("atendida_em"):
        await http.delete(f"{ARI}/channels/{cid}")
        await finalizar(cid, "outro", None, "nenhum evento recebido")
        return
    await asyncio.sleep(DURACAO_MAXIMA)
    if cid in chamadas:
        await http.delete(f"{ARI}/channels/{cid}")


async def desligar(cid):
    """Desliga uma chamada atendida so depois da duracao minima (evita chamada curta)."""
    c = chamadas.get(cid)
    if c and c.get("atendida_em"):
        falta = DURACAO_MINIMA - (time.time() - c["atendida_em"])
        if falta > 0:
            await asyncio.sleep(falta)
    if cid in chamadas:
        await http.delete(f"{ARI}/channels/{cid}")


# ------------------------------ roteiro ------------------------------
async def tocar(cid, audio, etapa):
    c = chamadas[cid]
    pb_id = f"pb-{uuid.uuid4().hex[:10]}"
    c["playback"] = {"id": pb_id, "etapa": etapa}
    await http.post(f"{ARI}/channels/{cid}/play",
                    params={"media": f"sound:{audio}", "playbackId": pb_id})


async def fazer_pergunta(cid):
    c = chamadas[cid]
    c["estado"] = "pergunta"
    await tocar(cid, perguntas[c["idx"]]["audio"], "pergunta")


async def iniciar_pesquisa(cid):
    c = chamadas[cid]
    c["idx"], c["repeticoes"] = 0, 0
    if campanha["ura_abertura"]:
        c["estado"] = "abertura"
        await tocar(cid, campanha["ura_abertura"], "abertura")
    else:
        await fazer_pergunta(cid)


async def encerrar(cid):
    c = chamadas[cid]
    c["estado"] = "fim"
    if c.get("timer"):
        c["timer"].cancel()
    if campanha["ura_encerramento"]:
        await tocar(cid, campanha["ura_encerramento"], "encerramento")
    else:
        asyncio.create_task(desligar(cid))


async def esperar_tecla(cid, pb_id, segundos):
    await asyncio.sleep(segundos)
    c = chamadas.get(cid)
    if not c or c["estado"] != "pergunta" or (c.get("playback") or {}).get("id") != pb_id:
        return
    c["repeticoes"] += 1
    if c["repeticoes"] > MAX_REPETICOES:
        print(f"{agora()}  {c['numero']}: sem resposta na pergunta {c['idx'] + 1}, encerrando")
        await encerrar(cid)
    else:
        await fazer_pergunta(cid)


async def tecla(cid, digito):
    c = chamadas[cid]
    if c["estado"] != "pergunta":
        return
    if c.get("timer"):
        c["timer"].cancel()
    pb = c.get("playback")
    if pb and pb["etapa"] in ("pergunta", "invalida"):
        await http.delete(f"{ARI}/playbacks/{pb['id']}")      # interrompe o audio
        c["playback"] = None
    p = perguntas[c["idx"]]
    if digito in p["teclas_validas"]:
        await db.execute("""INSERT INTO ura_respostas (tentativa_id, ordem, tecla)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (tentativa_id, ordem)
                            DO UPDATE SET tecla = EXCLUDED.tecla, respondida_em = now()""",
                         (c["tentativa_id"], p["ordem"], digito))
        c["respostas"][p["ordem"]] = digito
        print(f"{agora()}  {c['numero']}: P{p['ordem']} = {digito}")
        c["idx"] += 1
        c["repeticoes"] = 0
        if c["idx"] < len(perguntas):
            await fazer_pergunta(cid)
        else:
            await encerrar(cid)
    else:
        c["repeticoes"] += 1
        print(f"{agora()}  {c['numero']}: tecla invalida '{digito}' na P{p['ordem']}")
        if c["repeticoes"] > MAX_REPETICOES:
            await encerrar(cid)
        elif campanha["ura_invalida"]:
            await tocar(cid, campanha["ura_invalida"], "invalida")
        else:
            await fazer_pergunta(cid)


# ------------------------------ fim da chamada ------------------------------
async def finalizar(cid, resultado, causa_codigo, causa_texto):
    c = chamadas.pop(cid, None)
    if c is None:
        return
    if c.get("timer"):
        c["timer"].cancel()
    await liberar_canal(cid)
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
        if resultado in ("respondente", "atendida") and contato["lista_contato_id"]:
            await cur.execute("""UPDATE campanha_contatos
                                 SET status = 'finalizado', atualizado_em = now(),
                                     ultimo_resultado = 'atendido em outro telefone'
                                 WHERE campanha_id = %s AND lista_contato_id = %s
                                   AND id <> %s AND status = 'pendente'""",
                              (args.campanha, contato["lista_contato_id"], contato["id"]))

    resp = " ".join(f"P{k}={v}" for k, v in sorted(c["respostas"].items())) or "-"
    quando = "" if status == "finalizado" else f" | nova tentativa em {regra['intervalo']}"
    print(f"{agora()}  >>> {c['numero']}: {resultado} | respostas: {resp} | "
          f"conversa {conversa or 0}s -> {motivo} [{status}]{quando}")


# ------------------------------ eventos ------------------------------
async def tratar(ev):
    tipo = ev["type"]
    if tipo in IGNORAR:
        return

    if tipo == "PlaybackFinished":
        pb = ev["playback"]
        cid = pb["target_uri"].split(":", 1)[1]
        c = chamadas.get(cid)
        if not c or (c.get("playback") or {}).get("id") != pb["id"]:
            return                                    # audio antigo, ja interrompido
        etapa = c["playback"]["etapa"]
        if etapa in ("abertura", "invalida"):
            await fazer_pergunta(cid)
        elif etapa == "pergunta":
            c["timer"] = asyncio.create_task(
                esperar_tecla(cid, pb["id"], perguntas[c["idx"]]["timeout_s"]))
        elif etapa == "encerramento":
            asyncio.create_task(desligar(cid))
        return

    canal = ev.get("channel", {})
    cid = canal.get("id", "")
    c = chamadas.get(cid)
    if not c:
        return

    if tipo == "StasisStart":
        a = ev.get("args") or []
        if a[:1] == ["atendeu"]:
            c["atendida_em"] = time.time()
            c["estado"] = "amd"
            await db.execute("UPDATE tentativas SET atendida_em = now() WHERE id = %s",
                             (c["tentativa_id"],))
            if campanha["ura_amd"]:
                # sai do app para rodar a deteccao de secretaria no dialplan
                await http.post(f"{ARI}/channels/{cid}/continue",
                                params={"context": "ura-amd", "extension": "s", "priority": 1})
            else:
                await iniciar_pesquisa(cid)
        elif a[:1] == ["amd"]:
            status = a[1] if len(a) > 1 else ""
            causa = a[2] if len(a) > 2 else ""
            await db.execute("UPDATE tentativas SET amd_status = %s, amd_causa = %s WHERE id = %s",
                             (status, causa, c["tentativa_id"]))
            print(f"{agora()}  {c['numero']} atendeu | deteccao: {status} ({causa})")
            if status == "MACHINE":
                c["caixa_postal"] = True
                print(f"{agora()}  {c['numero']}: caixa postal, segurando ate {DURACAO_MINIMA}s")
                asyncio.create_task(desligar(cid))
            else:                                     # HUMAN ou NOTSURE: segue a pesquisa
                await iniciar_pesquisa(cid)

    elif tipo == "ChannelDtmfReceived":
        await tecla(cid, ev["digit"])

    elif tipo == "ChannelDestroyed":
        if c.get("atendida_em"):
            if c.get("caixa_postal"):
                resultado = "caixa_postal"
            elif c["respostas"]:
                resultado = "respondente"
            else:
                resultado = "atendida"
            await finalizar(cid, resultado, ev.get("cause"), ev.get("cause_txt"))
        else:
            await finalizar(cid, classificar(ev.get("cause")), ev.get("cause"),
                            ev.get("cause_txt"))


async def relogio():
    n = 0
    while True:
        await asyncio.sleep(1)
        n += 1
        if n % 5 == 0:
            await carregar_campanha()
        await discar()


async def main():
    global http, db, args, perguntas, DURACAO_MINIMA
    p = argparse.ArgumentParser(description="URA de pesquisa")
    p.add_argument("--campanha", type=int, required=True)
    args = p.parse_args()

    db = await psycopg.AsyncConnection.connect("dbname=discadora", autocommit=True)
    await carregar_campanha()
    if campanha is None:
        raise SystemExit(f"Campanha {args.campanha} nao existe")
    if campanha["tipo"] != "ura":
        raise SystemExit(f"A campanha {args.campanha} e do tipo '{campanha['tipo']}', nao 'ura'")
    async with db.cursor(row_factory=dict_row) as cur:
        await cur.execute("""SELECT ordem, audio, teclas_validas, timeout_s FROM ura_perguntas
                             WHERE campanha_id = %s ORDER BY ordem""", (args.campanha,))
        perguntas = await cur.fetchall()
        await cur.execute("SELECT valor FROM config WHERE chave = 'duracao_minima_s'")
        linha = await cur.fetchone()
        if linha:
            DURACAO_MINIMA = int(linha["valor"])
        await cur.execute("""SELECT bool_and(position('@' in endpoint) = 0) AS so_lab
                             FROM troncos WHERE ativo""")
        so_lab = (await cur.fetchone())["so_lab"]
        await cur.execute("""SELECT bool_or(length(telefone) >= 10) AS reais FROM campanha_contatos
                             WHERE campanha_id = %s AND status <> 'finalizado'""", (args.campanha,))
        tem_reais = (await cur.fetchone())["reais"]
    if not perguntas:
        raise SystemExit("A campanha nao tem perguntas cadastradas em ura_perguntas.")
    if so_lab and tem_reais:
        raise SystemExit("A campanha tem telefones reais, mas so ha troncos de laboratorio ativos.")

    # recuperacao de queda
    await db.execute("""UPDATE campanha_contatos SET status = 'pendente'
                        WHERE campanha_id = %s AND status = 'discando'""", (args.campanha,))
    await db.execute("DELETE FROM canais_em_uso WHERE tipo = 'ura'")

    url = f"ws://127.0.0.1:8088/ari/events?app={APP}&api_key={USUARIO}:{SENHA}"
    async with httpx.AsyncClient(auth=(USUARIO, SENHA), timeout=10) as cliente_http:
        http = cliente_http
        async with websockets.connect(url) as ws:
            print(f"URA no ar | campanha {campanha['id']} '{campanha['nome']}' | "
                  f"{len(perguntas)} perguntas | ate {campanha['max_canais']} canais")
            asyncio.create_task(relogio())
            async for msg in ws:
                await tratar(json.loads(msg))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nEncerrado.")
