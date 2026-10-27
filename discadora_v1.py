import asyncio, csv, json, time, uuid
from datetime import datetime
from pathlib import Path
import httpx, websockets

# ================= configuracao =================
ARI = "http://127.0.0.1:8088/ari"
USUARIO = "discadora"
SENHA = (Path.home() / "lab" / "ari_senha.txt").read_text().strip()
APP = "discadora"
MODO_LAB = True              # True: disca para ramais | False: disca pelo tronco
TRONCO = "tronco_operadora"  # nome do endpoint do tronco (Fase B)
BINA = "4130000000"
TEMPO_TOQUE = 25             # segundos tocando antes de desistir
PAUSA_POS_CHAMADA = 5        # "tabulacao": descanso do operador apos uma conversa
ARQ_RESULTADOS = Path.home() / "lab" / "resultados.csv"
IGNORAR = {"ChannelVarset", "ChannelDialplan"}

# 6009 nao existe: serve para testar numero invalido
MAILING = ["6001", "6002", "6001", "6009", "6002"]

# ================= estado =================
fila = asyncio.Queue()
operadores = {}   # canal do operador -> {"nome", "livre"}
tentativas = {}   # canal do cliente  -> dados da tentativa
http = None

def agora():
    return datetime.now().strftime("%H:%M:%S")

def classificar(causa):
    # causas Q.850 -> status do mailing (vamos validar esta tabela nos testes)
    if causa == 17:              return "ocupado"
    if causa in (18, 19):        return "nao_atende"
    if causa == 21:              return "recusou"
    if causa == 20:              return "indisponivel"
    if causa in (1, 3, 28):      return "invalido"
    if causa in (34, 38, 41, 42): return "falha_rede"
    return "outro"

def registrar(t, resultado, causa):
    novo = not ARQ_RESULTADOS.exists()
    dur = round(time.time() - t["atendida_em"]) if t.get("atendida_em") else 0
    with ARQ_RESULTADOS.open("a", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if novo:
            w.writerow(["data_hora", "numero", "resultado", "causa", "conversa_s", "gravacao"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), t["numero"],
                    resultado, causa, dur, t.get("gravacao", "")])
    print(f"{agora()}  >>> RESULTADO {t['numero']}: {resultado} (causa {causa}, conversa {dur}s)")

async def finalizar(cli_id, resultado, causa):
    t = tentativas.pop(cli_id, None)
    if t is None:
        return                                   # ja finalizada
    if t.get("ponte"):
        await http.delete(f"{ARI}/bridges/{t['ponte']}")
    registrar(t, resultado, causa)
    pausa = PAUSA_POS_CHAMADA if t.get("atendida_em") else 0
    asyncio.create_task(liberar_operador(t["operador"], pausa))
    if fila.empty() and not tentativas:
        print(f"{agora()}  === Mailing concluido. Resultados em {ARQ_RESULTADOS} ===")

async def liberar_operador(op_id, pausa):
    await asyncio.sleep(pausa)
    op = operadores.get(op_id)
    if op:
        op["livre"] = True
        print(f"{agora()}  operador {op['nome']} disponivel")
    await discar_para_livres()

async def vigiar(cli_id):
    # rede de seguranca: se nenhum evento encerrar a tentativa, encerra aqui
    await asyncio.sleep(TEMPO_TOQUE + 15)
    t = tentativas.get(cli_id)
    if t and not t.get("atendida_em"):
        await http.delete(f"{ARI}/channels/{cli_id}")
        await finalizar(cli_id, "sem_evento", "nenhum evento recebido")

async def discar_para_livres():
    for op_id, op in list(operadores.items()):
        if not op["livre"] or fila.empty():
            continue
        numero = fila.get_nowait()
        cli_id = f"cli-{uuid.uuid4().hex[:12]}"
        op["livre"] = False
        tentativas[cli_id] = {"numero": numero, "operador": op_id, "ponte": None}
        endpoint = f"PJSIP/{numero}" if MODO_LAB else f"PJSIP/{numero}@{TRONCO}"
        print(f"{agora()}  discando {numero} para {op['nome']}")
        r = await http.post(f"{ARI}/channels", params={
            "endpoint": endpoint, "app": APP, "appArgs": "cliente",
            "channelId": cli_id, "callerId": BINA, "timeout": TEMPO_TOQUE})
        if r.status_code >= 400:
            await finalizar(cli_id, "erro_originacao", f"HTTP {r.status_code} {' '.join(r.text.split())[:60]}")
        else:
            asyncio.create_task(vigiar(cli_id))

async def tratar(ev):
    tipo = ev["type"]
    if tipo in IGNORAR:
        return
    canal = ev.get("channel", {})
    cid = canal.get("id", "")
    papel = "cliente" if cid in tentativas else ("operador" if cid in operadores else "")
    extra = f" causa={ev['cause']} {ev.get('cause_txt', '')}" if "cause" in ev else ""
    print(f"{agora()}    [{tipo}] {canal.get('name', '-')} {papel}{extra}")

    if tipo == "StasisStart":
        args = ev.get("args") or [""]
        if args[0] == "operador":
            await http.post(f"{ARI}/channels/{cid}/answer")
            operadores[cid] = {"nome": canal["name"], "livre": True}
            print(f"{agora()}  operador {canal['name']} disponivel")
        elif args[0] == "cliente" and cid in tentativas:
            t = tentativas[cid]
            if t["operador"] not in operadores:          # operador saiu: abandono
                t["resultado_forcado"] = "abandono"
                await http.delete(f"{ARI}/channels/{cid}")
            else:
                ponte = (await http.post(f"{ARI}/bridges", params={"type": "mixing"})).json()["id"]
                t.update(ponte=ponte, atendida_em=time.time(), gravacao=cid)
                await http.post(f"{ARI}/bridges/{ponte}/addChannel",
                                params={"channel": f"{t['operador']},{cid}"})
                await http.post(f"{ARI}/bridges/{ponte}/record",
                                params={"name": cid, "format": "wav"})
                print(f"{agora()}  {t['numero']} ATENDEU -> conectado ao operador")

    elif tipo == "StasisEnd":
        if cid in tentativas:                            # cliente atendido saiu
            await finalizar(cid, tentativas[cid].get("resultado_forcado", "atendida"), "-")
        elif cid in operadores:                          # operador saiu
            operadores.pop(cid)
            print(f"{agora()}  operador {canal['name']} saiu")
            for cli_id, t in list(tentativas.items()):
                if t["operador"] == cid and t.get("ponte"):
                    t["resultado_forcado"] = "operador_desligou"
                    await http.delete(f"{ARI}/channels/{cli_id}")

    elif tipo == "ChannelDestroyed" and cid in tentativas:
        t = tentativas[cid]
        causa = ev.get("cause")
        if t.get("atendida_em"):
            await finalizar(cid, t.get("resultado_forcado", "atendida"), "-")
        else:
            await finalizar(cid, classificar(causa), f"{causa} {ev.get('cause_txt', '')}")

    await discar_para_livres()

async def main():
    global http
    for n in MAILING:
        fila.put_nowait(n)
    url = f"ws://127.0.0.1:8088/ari/events?app={APP}&api_key={USUARIO}:{SENHA}"
    async with httpx.AsyncClient(auth=(USUARIO, SENHA), timeout=10) as cliente_http:
        http = cliente_http
        async with websockets.connect(url) as ws:
            print(f"Discadora no ar: {fila.qsize()} numeros no mailing.")
            print("Para o operador ficar disponivel: no navegador (7001), ligue para 900.")
            async for msg in ws:
                await tratar(json.loads(msg))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nEncerrado.")
