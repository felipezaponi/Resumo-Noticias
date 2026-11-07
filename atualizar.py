"""Lê as fontes, filtra pela blacklist, resume com IA (Gemini) e salva em noticias.json."""
import json, os, re, sys, time, html, unicodedata, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import feedparser

MODELO = "gemini-3.8-flash"      # se o Google trocar o nome do modelo, altere aqui
MAX_POR_FONTE = 10               # quantos itens mais recentes olhar em cada fonte
TAMANHO_LOTE = 4                 # itens por chamada de IA
DIAS_GUARDAR = 7
MAX_ITENS = 300
UA = "Mozilla/5.0 (compatible; MeuResumoDeNoticias/1.0)"
API_KEY = os.environ.get("GEMINI_API_KEY", "")


def ler_lista(caminho):
    with open(caminho, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def carregar_fontes():
    fontes = []
    for linha in ler_lista("fontes.txt"):
        if linha.lower().startswith("r/"):
            sub = linha.split()[0]
            fontes.append((sub, f"https://www.reddit.com/{sub}/.rss"))
        elif "|" in linha:
            nome, url = [p.strip() for p in linha.split("|", 1)]
            fontes.append((nome, url))
        elif linha.startswith("http"):
            fontes.append((re.sub(r"^https?://(www\.)?", "", linha).split("/")[0], linha))
    return fontes


def normalizar(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def bate_blacklist(texto, termos):
    t = normalizar(texto)
    return any(re.search(r"\b" + re.escape(normalizar(x)) + r"\b", t) for x in termos)


def limpar(h):
    h = re.sub(r"<[^>]+>", " ", h or "")
    h = re.sub(r"\s+", " ", html.unescape(h)).strip()
    return re.sub(r"submitted by.*$", "", h).strip()


def achar_imagem(e):
    for campo in ("media_thumbnail", "media_content"):
        for m in e.get(campo, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in e.get("enclosures", []) or []:
        if "image" in enc.get("type", "") and enc.get("href"):
            return enc["href"]
    bruto = (e.get("content", [{}])[0].get("value", "") if e.get("content") else "") + e.get("summary", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', bruto)
    return html.unescape(m.group(1)) if m else None


def data_iso(e):
    p = e.get("published_parsed") or e.get("updated_parsed")
    d = datetime(*p[:6], tzinfo=timezone.utc) if p else datetime.now(timezone.utc)
    return d.isoformat()


def gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }).encode()
    for tentativa in range(3):
        try:
            req = urllib.request.Request(url, corpo, {"Content-Type": "application/json", "x-goog-api-key": API_KEY})
            with urllib.request.urlopen(req, timeout=60) as r:
                dados = json.load(r)
            return json.loads(dados["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as err:
            print(f"  IA erro {err.code}, tentando de novo...")
            time.sleep(35 if err.code == 429 else 5)
        except Exception as err:
            print(f"  IA falhou: {err}")
            time.sleep(5)
    return None


def processar_lote(lote, blacklist):
    lista = "\n".join(f"- {t}" for t in blacklist)
    itens = "\n\n".join(
        f"ID {i}\nFonte: {x['fonte']}\nTítulo: {x['titulo']}\nTexto: {x['texto'][:1500]}" for i, x in enumerate(lote)
    )
    prompt = f"""Você filtra e resume notícias para um leitor brasileiro que NÃO quer spoilers.

ASSUNTOS PROIBIDOS (blacklist):
{lista}

Para cada item abaixo, decida:
1. "bloquear": true se o item mencionar, tratar, comentar ou fizer QUALQUER alusão aos assuntos proibidos, mesmo indireta (sinônimos, personagens, estúdio, trailer, vazamento, comentários sobre o tema, siglas). Na dúvida, bloqueie.
2. "resumo": se NÃO bloquear, resuma em português, direto, entre 400 e 600 caracteres, com os fatos principais. O resumo não pode citar nem insinuar nenhum assunto proibido.

Responda APENAS com um JSON no formato:
[{{"id": 0, "bloquear": false, "resumo": "..."}}, ...]
(use "resumo": "" quando bloquear). Inclua todos os IDs.

ITENS:
{itens}"""
    resp = gemini(prompt)
    resultado = {}
    if isinstance(resp, list):
        for r in resp:
            try:
                resultado[int(r["id"])] = r
            except Exception:
                pass
    return resultado


def main():
    if not API_KEY:
        sys.exit("ERRO: chave GEMINI_API_KEY não configurada.")
    blacklist = ler_lista("blacklist.txt")
    try:
        with open("noticias.json", encoding="utf-8") as f:
            base = json.load(f)
    except Exception:
        base = {"itens": [], "bloqueados": []}
    itens = base.get("itens", [])
    bloqueados = set(base.get("bloqueados", []))
    conhecidos = {i["link"] for i in itens} | bloqueados

    pendentes = []
    fontes = carregar_fontes()
    for nome, url in fontes:
        try:
            feed = feedparser.parse(url, agent=UA)
            if not feed.entries and "reddit.com" in url:
                feed = feedparser.parse(url.replace("www.reddit.com", "old.reddit.com"), agent=UA)
            if not feed.entries:
                print(f"AVISO: nenhum item em {nome} (status {feed.get('status')})")
            for e in feed.entries[:MAX_POR_FONTE]:
                link = e.get("link")
                if not link or link in conhecidos:
                    continue
                titulo = limpar(e.get("title", ""))
                texto = limpar(e.get("summary", "")) or limpar(
                    e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
                if bate_blacklist(titulo + " " + texto, blacklist):
                    bloqueados.add(link)
                    continue
                pendentes.append({"fonte": nome, "link": link, "titulo": titulo, "texto": texto,
                                  "imagem": achar_imagem(e), "data": data_iso(e)})
        except Exception as err:
            print(f"AVISO: falha em {nome}: {err}")

    print(f"{len(pendentes)} itens novos para a IA analisar")
    for i in range(0, len(pendentes), TAMANHO_LOTE):
        lote = pendentes[i:i + TAMANHO_LOTE]
        res = processar_lote(lote, blacklist)
        for idx, x in enumerate(lote):
            r = res.get(idx)
            if r is None:
                continue  # IA falhou: não publica (proteção contra spoiler) e tenta de novo na próxima rodada
            if r.get("bloquear") or not r.get("resumo"):
                bloqueados.add(x["link"])
            else:
                itens.append({"fonte": x["fonte"], "link": x["link"], "titulo": x["titulo"],
                              "resumo": r["resumo"].strip(), "imagem": x["imagem"], "data": x["data"]})

    # Reaplica a blacklist por palavra nos itens antigos (caso você tenha adicionado palavras novas)
    itens = [i for i in itens if not bate_blacklist(i["titulo"] + " " + i["resumo"], blacklist)]
    corte = (datetime.now(timezone.utc) - timedelta(days=DIAS_GUARDAR)).isoformat()
    itens = sorted([i for i in itens if i["data"] >= corte], key=lambda i: i["data"], reverse=True)[:MAX_ITENS]

    with open("noticias.json", "w", encoding="utf-8") as f:
        json.dump({"atualizado_em": datetime.now(timezone.utc).isoformat(),
                   "fontes": [n for n, _ in fontes], "itens": itens,
                   "bloqueados": list(bloqueados)[-3000:]}, f, ensure_ascii=False, indent=1)
    print(f"Pronto: {len(itens)} itens publicados.")


main()
