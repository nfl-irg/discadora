import asyncio, json
from pathlib import Path
import httpx, websockets

ARI = "http://127.0.0.1:8088/ari"
USUARIO = "discadora"
SENHA = (Path.home() / "lab" / "ari_senha.txt").read_text().strip()
APP = "ola"
IGNORAR = {"ChannelVarset", "ChannelDialplan"}   # eventos "barulhentos"

async def main():
    url = f"ws://127.0.0.1:8088/ari/events?app={APP}&api_key={USUARIO}:{SENHA}"
    async with httpx.AsyncClient(auth=(USUARIO, SENHA), timeout=10) as http:
        async with websockets.connect(url) as ws:
            print("Conectado ao Asterisk. Ligue para 200 no MicroSIP. (Ctrl+C para sair)")
            async for msg in ws:
                ev = json.loads(msg)
                tipo = ev["type"]
                if tipo in IGNORAR:
                    continue
                canal = ev.get("channel", {})
                print(f"[evento] {tipo:<22} canal={canal.get('name', '-')}")

                if tipo == "StasisStart":
                    # chegou uma chamada: atende e toca "hello world"
                    cid = canal["id"]
                    await http.post(f"{ARI}/channels/{cid}/answer")
                    await http.post(f"{ARI}/channels/{cid}/play",
                                    params={"media": "sound:hello-world"})

                elif tipo == "ChannelDtmfReceived":
                    # apertaram uma tecla: fala o numero, ou desliga com #
                    cid, digito = canal["id"], ev["digit"]
                    print(f"           tecla apertada: {digito}")
                    if digito == "#":
                        await http.delete(f"{ARI}/channels/{cid}")
                    elif digito.isdigit():
                        await http.post(f"{ARI}/channels/{cid}/play",
                                        params={"media": f"sound:digits/{digito}"})

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nEncerrado.")
