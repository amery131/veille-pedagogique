#!/usr/bin/env python3
"""Met à jour le tableau de bord Vivaldi à partir de flux publics.

Mode libre (par défaut) : extraits RSS + pistes pédagogiques déterministes.
Mode enrichi (si OPENAI_API_KEY) : sélection et reformulation assistées par IA.
Aucune donnée personnelle ni conversation ChatGPT n'est consultée.
"""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import html
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
PARIS = ZoneInfo("Europe/Paris")
WINDOW_HOURS = 48
NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(hours=WINDOW_HOURS)
OUTPUT_JSON = ROOT / "veille.json"
OUTPUT_RSS = ROOT / "veille.xml"
USER_AGENT = "VeillePedagogiqueVivaldi/1.0 (agrégateur de flux publics)"
MAX_PER_SOURCE = 3

FEEDS = [
    ("Édubase · physique-chimie", "https://edubase.eduscol.education.fr/rss/rss.xml?q=&discipline%5B%5D=Physique+%2F+Chimie"),
    ("Édubase · mathématiques", "https://edubase.eduscol.education.fr/rss/rss.xml?q=&discipline%5B%5D=Math%C3%A9matiques"),
    ("Les Cahiers pédagogiques", "https://www.cahiers-pedagogiques.com/feed/"),
    ("Le Café pédagogique", "https://www.cafepedagogique.net/feed/"),
    ("Éduveille / IFÉ", "https://eduveille.hypotheses.org/feed"),
    ("Ludomag", "https://www.ludomag.com/feed/"),
    ("Interstices", "https://interstices.info/feed/"),
]

TOPICS = {
    "Physique-chimie": ["physique", "chimie", "scientifique", "expérience", "expérimentation", "laboratoire", "mécanique", "énergie", "électricité", "astronomie", "optique", "matière", "molecule", "molécule", "acoustique", "électrique"],
    "Mathématiques": ["mathématique", "maths", "proportionnalité", "géométrie", "calcul", "statistique", "équation", "modélisation", "nombre", "mesure", "probabilité", "algèbre"],
    "IA et numérique": ["intelligence artificielle", "ia générative", "apprentissage automatique", "numérique éducatif", "pix+", "chatgpt", "algorithme", "modèle de langage", "données", "simulation", "ressources numériques"],
}
TEACHING_TERMS = ["enseign", "classe", "collège", "lycée", "élève", "pédagog", "didact", "activité", "ressource", "éducation", "programme", "formation", "expériment", "évaluat", "apprenti", "école", "esprit critique", "compétence", "science"]
EXCLUDE_TERMS = ["cinéma :", "films à voir", "exposition au musée", "vacances d'été", "mobilisation lycéenne", "salaires enseignants", "offre d'emploi", "adresses de restaurants", "politique étrangère", "appel d'offres"]


def clean_text(raw: str, limit: int = 750) -> str:
    """N'utilise que le texte de l'extrait, sans télécharger/reproduire l'article entier."""
    soup = BeautifulSoup(raw or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    txt = html.unescape(soup.get_text(" ", strip=True))
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[: limit - 1].rstrip() + "…") if len(txt) > limit else txt


def date_of(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        v = entry.get(key)
        if v:
            try:
                return datetime.fromtimestamp(calendar.timegm(v), tz=timezone.utc)
            except Exception:
                pass
    return None


def normalize_url(url):
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith("utm_") and k not in ("fbclid", "gclid")])
    return urlunparse((u.scheme, u.netloc.lower(), u.path.rstrip("/") or "/", "", query, ""))


def category_for(text):
    t = text.lower()
    scores = {name: sum(2 if len(word) > 5 else 1 for word in words if word in t) for name, words in TOPICS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "Pédagogie"


def score_for(item):
    full = (item["title"] + " " + item["excerpt"][:400]).lower()
    title = item["title"].lower()
    # Favorise les travaux concrets en sciences et la didactique des disciplines.
    score = sum(3 for term in TEACHING_TERMS if term in full)
    score += sum(5 for term in ("physique", "chimie", "mathématique", "enseignement scientifique", "didactique", "séance", "scénario pédagogique", "modélisation") if term in title)
    score += sum(2 for term in ("tutorat", "esprit critique", "démarche", "investigation", "classe puzzle", "expérimentation", "biais", "méthode scientifique", "données") if term in full)
    score -= sum(10 for term in EXCLUDE_TERMS if term in full)
    if item["source"].startswith("Édubase"):
        score += 5
    # Un bonus mesuré pour la fraîcheur : les 48 h restent une condition impérative.
    age_hours = max(0, (NOW - item["published_utc"]).total_seconds() / 3600)
    score += 2 if age_hours < 24 else 0
    return score


def fetch_feed(feed):
    source, url = feed
    resp = requests.get(url, timeout=(8, 18), headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,application/atom+xml,application/xml,*/*"})
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        print(f"[INFO] Aucun élément RSS chez {source} (code HTTP {resp.status_code})")
    collected = []
    for entry in parsed.entries[:100]:
        pdate = date_of(entry)
        if pdate is None or pdate < SINCE or pdate > NOW + timedelta(hours=1):
            continue
        url = normalize_url(entry.get("link", ""))
        title = clean_text(entry.get("title", ""), 180)
        if not url or not title:
            continue
        excerpt = clean_text(entry.get("summary", "") or entry.get("description", ""), 700)
        item = {"source": source, "title": title, "url": url, "published_utc": pdate, "excerpt": excerpt}
        item["category"] = category_for(title + " " + excerpt)
        item["score"] = score_for(item)
        if item["score"] >= 5:
            collected.append(item)
    return collected


def fallback_summary(excerpt, title):
    """Extraction sobre, jamais présentée comme lecture intégrale de l'article."""
    text = excerpt.strip() or f"Cette ressource présente « {title} »."
    text = re.sub(r"\s+", " ", text)
    # Deux phrases au plus, ou 245 caractères si l'extrait est peu ponctué.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ŸÉÈÊÎ])", text)
    part = " ".join(sentences[:2]).strip()
    if len(part) > 260:
        part = part[:257].rsplit(" ", 1)[0].rstrip(".,; ") + "…"
    return part


def teaching_interest(category, title):
    t = title.lower()
    if "évaluation" in t or "erreur" in t:
        return "Pour enrichir la correction, l'évaluation formative et l'analyse des erreurs des élèves."
    if "ia" in t or "intelligence artificielle" in t or "données" in t:
        return "Pour préparer une activité d'esprit critique sur les données, les biais et les modèles en enseignement scientifique."
    if "Physique-chimie" == category:
        return "À examiner pour construire ou adapter une activité expérimentale de physique-chimie au collège."
    if "Mathématiques" == category:
        return "Pour renforcer les liens entre calcul, traitement des données et raisonnement en sciences."
    if "IA et numérique" == category:
        return "Pour identifier un usage raisonné du numérique, à vérifier avant un emploi en classe."
    return "Pour nourrir la préparation de séances différenciées et la réflexion sur l'apprentissage des élèves."


def pick_ranked(items, limit=5):
    ordered = sorted(items, key=lambda a: (a["score"], a["published_utc"]), reverse=True)
    selected, quotas = [], {}
    for a in ordered:
        if quotas.get(a["source"], 0) >= MAX_PER_SOURCE:
            continue
        selected.append(a)
        quotas[a["source"]] = quotas.get(a["source"], 0) + 1
        if len(selected) >= limit:
            break
    return selected


def ai_selection(candidates):
    """Option payante facultative. Les liens, dates et sources sont repris uniquement des RSS."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not candidates:
        return None
    short = candidates[:25]
    catalog = [
        {"index": i, "date": c["published_utc"].isoformat(), "source": c["source"], "title": c["title"], "excerpt": c["excerpt"][:450]}
        for i, c in enumerate(short)
    ]
    system = (
        "Vous êtes documentaliste spécialiste de la pédagogie en France, physique-chimie collège "
        "et enseignement scientifique en première. Sélectionnez jusqu'à 5 articles les plus UTILES "
        "parmi la liste fournie, en privilégiant physique-chimie, maths, scénarios didactiques "
        "et esprit critique. Variez les sources. Il est interdit d'inventer un article, "
        "des détails, des résultats d'expérimentation ou une URL. Pour chacun, résumez "
        "uniquement les informations de l'extrait en 1 à 2 phrases (<=240 caractères) "
        "et rédigez une unique phrase d'intérêt concret pour l'enseignante (<=170 caractères). "
        "Si les extraits ne permettent pas un résumé fiable, dites-le clairement. "
        "Répondez uniquement dans le schéma JSON demandé."
    )
    schema = {
        "type": "object", "properties": {"articles": {"type": "array", "items": {
            "type": "object", "properties": {"index": {"type": "integer"}, "summary": {"type": "string"}, "interest": {"type": "string"}},
            "required": ["index", "summary", "interest"], "additionalProperties": False
        }}}, "required": ["articles"], "additionalProperties": False
    }
    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "input": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(catalog, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "selection_veille", "strict": True, "schema": schema}},
        "max_output_tokens": 1600
    }
    try:
        resp = requests.post("https://api.openai.com/v1/responses", json=body,
                             headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=75)
        resp.raise_for_status()
        payload = resp.json()
        text_parts = [c.get("text", "") for block in payload.get("output", []) for c in block.get("content", []) if c.get("type") == "output_text"]
        results = json.loads("".join(text_parts))["articles"]
        final, used, source_counts = [], set(), {}
        for result in results:
            i = result.get("index")
            if not isinstance(i, int) or not 0 <= i < len(short) or i in used:
                continue
            candidate = short[i]
            if source_counts.get(candidate["source"], 0) >= MAX_PER_SOURCE:
                continue
            summary, interest = result.get("summary", "").strip(), result.get("interest", "").strip()
            if not summary or not interest:
                continue
            final.append((candidate, summary[:270], interest[:180]))
            used.add(i)
            source_counts[candidate["source"]] = source_counts.get(candidate["source"], 0) + 1
            if len(final) == 5:
                break
        if final:
            print(f"[OK] Sélection assistée par IA : {len(final)} ressources")
            return final
    except Exception as e:
        print(f"[ATTENTION] API IA indisponible, sélection sans IA : {str(e)[:220]}")
    return None


def to_output(source, summary, interest):
    return {
        "title": source["title"], "url": source["url"], "source": source["source"],
        "date": source["published_utc"].astimezone(PARIS).date().isoformat(),
        "category": source["category"], "summary": summary, "interest": interest
    }


def write_rss(data):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for name, value in (("title", "Veille pédagogique : sélection commentée"),
                        ("link", os.getenv("SITE_URL", "https://example.org/veille-pedagogique/")),
                        ("description", "Jusqu'à cinq ressources récentes en physique-chimie, maths, pédagogie et enseignement scientifique"),
                        ("language", "fr-FR"),
                        ("lastBuildDate", format_datetime(NOW)),
                        ("generator", "Veille pédagogique Vivaldi")):
        ET.SubElement(channel, name).text = value
    for a in data["articles"]:
        entry = ET.SubElement(channel, "item")
        for tag, value in (("title", a["title"]), ("link", a["url"]),
                           ("guid", a["url"]), ("category", a["category"]),
                           ("description", a["summary"] + "\n\nIntérêt pédagogique : " + a["interest"])):
            ET.SubElement(entry, tag).text = value
        try:
            p = datetime.fromisoformat(a["date"]).replace(tzinfo=PARIS)
            ET.SubElement(entry, "pubDate").text = format_datetime(p)
        except ValueError:
            pass
    ET.ElementTree(rss).write(OUTPUT_RSS, encoding="utf-8", xml_declaration=True)


def main():
    print(f"Veille du {NOW.astimezone(PARIS):%Y-%m-%d %H:%M %Z}. Fenêtre : {WINDOW_HOURS} h.")
    articles, errors = [], []
    with ThreadPoolExecutor(max_workers=7) as pool:
        jobs = {pool.submit(fetch_feed, feed): feed[0] for feed in FEEDS}
        for future in as_completed(jobs):
            label = jobs[future]
            try:
                found = future.result()
                articles.extend(found)
                print(f"[OK] {label} : {len(found)} articles récents retenus")
            except Exception as e:
                errors.append(label)
                print(f"[ATTENTION] {label} inaccessible : {str(e)[:200]}")
    unique = {}
    for a in articles:
        key = a["url"]
        if key not in unique or a["score"] > unique[key]["score"]:
            unique[key] = a
    candidates = sorted(unique.values(), key=lambda a:(a["score"],a["published_utc"]), reverse=True)
    assisted = ai_selection(candidates)
    if assisted:
        chosen = [to_output(a, summ, interest) for a, summ, interest in assisted]
        mode, how = "automatic", "Sélection et résumés assistés par IA à partir d'extraits RSS, avec liens vers les sources."
    else:
        chosen = [to_output(a, fallback_summary(a["excerpt"], a["title"]), teaching_interest(a["category"], a["title"])) for a in pick_ranked(candidates)]
        mode, how = "automatic", "Sélection automatique à partir de flux RSS : les résumés utilisent les extraits publiés par les sources et les pistes de cours sont indicatives."
    message = how
    if not chosen:
        mode = "error" if len(errors)==len(FEEDS) else "empty"
        message = ("Impossible de lire les flux aujourd'hui. Aucune actualité n'est fabriquée." if mode=="error" else
                   "Aucune ressource suffisamment pertinente, datée et publiée dans les dernières 48 heures n'a été retenue aujourd'hui.")
    elif errors:
        message += f" {len(errors)} source(s) momentanément inaccessible(s)."
    if len(chosen) < 5 and chosen:
        message += f" Seulement {len(chosen)} ressource(s) retenue(s), plutôt que d'ajouter des articles anciens."
    data = {
        "generated_at": NOW.astimezone(PARIS).isoformat(timespec="minutes"),
        "mode": mode, "window_hours": WINDOW_HOURS, "message": message, "articles": chosen
    }
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_rss(data)
    print(f"[TERMINÉ] {len(chosen)} ressource(s) écrite(s) dans veille.json et veille.xml")


if __name__ == "__main__":
    main()
