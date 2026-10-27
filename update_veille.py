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
LOOKBACK_DAYS = 7
NOW = datetime.now(timezone.utc)
FRESH_SINCE = NOW - timedelta(hours=WINDOW_HOURS)
SINCE = NOW - timedelta(days=LOOKBACK_DAYS)
OUTPUT_JSON = ROOT / "veille.json"
OUTPUT_RSS = ROOT / "veille.xml"
USER_AGENT = "VeillePedagogiqueVivaldi/1.0 (agrégateur de flux publics)"
MAX_PER_SOURCE = 2

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
    score -= sum(7 for term in ("festival", "inscrits", "événement", "inscription", "rétro", "partenariat") if term in title)
    score += sum(7 for term in ("physique-chimie", "enseignement scientifique", "sciences physiques", "sciences expérimentales") if term in title)
    if item["source"].startswith("Édubase"):
        score += 5
    # La fraîcheur prime, mais une ressource plus ancienne peut compléter les cinq titres.
    age_hours = max(0, (NOW - item["published_utc"]).total_seconds() / 3600)
    score += 4 if age_hours < 48 else 0
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


# Ces activités sont des TRANSPOSITIONS proposées par le programme : elles ne sont
# pas présentées comme des séquences fournies par la source RSS.
SCENARIOS = {
    "cooperation": (
        ("4e", "35 min", "Faire expliquer une méthode par les pairs", "Groupes experts : distance, durée et conversion d’unités. Reformez des équipes mixtes pour résoudre un problème de vitesse.", "Une résolution justifiée et une grille de vérification mutuelle."),
        ("Première", "45 min", "Argumenter à partir de preuves", "Trois équipes confrontent des documents sur la nucléosynthèse, puis produisent une synthèse « trois preuves et une limite ».", "Une conclusion collective distinguant preuve et interprétation.")
    ),
    "ia": (
        ("4e", "25 min", "Contrôler une donnée avant de conclure", "À partir d’un jeu de distances et durées, repérer deux mesures douteuses puis discuter l’effet sur la vitesse moyenne calculée.", "Deux critères pour accepter une mesure."),
        ("Première", "45 min", "Identifier les limites d’un modèle", "Comparer un petit jeu de données et des valeurs issues d’un modèle ; relever les écarts et discuter les biais possibles.", "Une validation argumentée : domaine, écart et limite.")
    ),
    "inclusion": (
        ("5e", "30 min", "Différencier une démarche expérimentale", "Sur la reconnaissance de l’eau, proposer une consigne courte, un schéma légendé et des rôles tournants au laboratoire.", "Une observation et une conclusion formulées sans confondre les deux."),
        ("Première", "40 min", "Rendre l’argumentation accessible", "Faire étudier deux documents sur l’origine des éléments chimiques avec aides graduées et rôles de lecteur, vérificateur et rapporteur.", "Une justification commune et un retour individuel.")
    ),
    "assessment": (
        ("4e", "20 min", "Diagnostiquer les erreurs sur la vitesse", "Proposer trois réponses d’élèves, dont une avec une conversion erronée ; chacun choisit puis explique la correction.", "Un court bilan : erreur repérée, règle appliquée."),
        ("Première", "25 min", "Évaluer une interprétation scientifique", "Comparer deux conclusions contradictoires tirées d’un graphique de spectre ou d’énergie, puis décider laquelle les données soutiennent.", "Une justification écrite avec citation des données.")
    ),
    "astronomy": (
        ("5e", "35 min", "Lire des distances astronomiques", "Mission Cosmos : écrire 40 000 000 000 000 km en puissance de dix et comparer cette distance à celle des planètes et étoiles proches.", "Une comparaison argumentée utilisant l’ordre de grandeur."),
        ("Première", "45 min", "Relier observations et histoire de la matière", "Comparer des documents sur les abondances d’éléments et la nucléosynthèse pour expliquer l’origine de certains noyaux.", "Une frise scientifique avec trois preuves et une limite.")
    ),
    "matter": (
        ("5e", "35 min", "Distinguer dissolution et disparition", "Comparer eau, sel et sable dans deux béchers ; prévoir, observer et discuter ce qui reste présent après agitation.", "Schéma avant/après et conclusion sur la conservation de la matière."),
        ("Première", "45 min", "Relier modèle microscopique et matière", "Étudier un document sur les structures cristallines et confronter deux modèles d’organisation des entités.", "Un schéma légendé et un argument sur les limites du modèle.")
    ),
    "energy": (
        ("4e", "30 min", "Construire un bilan énergétique", "À partir d’une lampe, repérer le transfert d’énergie, puis comparer une estimation et une mesure simple.", "Un diagramme d’énergie et une phrase de bilan."),
        ("Première", "45 min", "Exploiter des données sur l’énergie solaire", "Étudier un graphique d’irradiance ou un bilan radiatif et expliquer un écart entre deux situations.", "Un raisonnement chiffré avec unités et incertitude discutée.")
    ),
    "math": (
        ("5e", "25 min", "Mobiliser les puissances de dix", "Classer des distances de la Mission Cosmos en écriture scientifique, puis justifier un classement par ordre de grandeur.", "Une conversion commentée et un classement vérifié."),
        ("Première", "40 min", "Interpréter un graphique scientifique", "Comparer deux séries de données et leurs représentations graphiques ; repérer ce que l’échelle change à l’interprétation.", "Une interprétation argumentée avec une limite.")
    ),
    "sound": (
        ("4e", "30 min", "Mesurer une fréquence", "Comparer deux sons enregistrés, lire une période sur un oscillogramme puis calculer la fréquence.", "Un calcul vérifié avec unités et interprétation."),
        ("Première", "45 min", "Relier son et musique", "Comparer des spectres de deux instruments jouant une même note, puis discuter la fréquence fondamentale et le timbre.", "Une conclusion fondée sur deux indices graphiques.")
    ),
    "default": (
        ("5e", "20 min", "Faire une place au doute scientifique", "À partir d’une affirmation sur l’eau, distinguer observation, hypothèse et preuve puis proposer un test témoin.", "Une question testable et une conclusion prudente."),
        ("Première", "30 min", "Interroger la fiabilité d’un document", "Étudier une affirmation scientifique et deux sources ; relever les données vérifiables, les inférences et ce qui manque.", "Une synthèse « deux preuves et une limite »." )
    )
}

def teaching_theme(title, excerpt):
    # Le titre détermine en priorité le domaine. Les mots-clés trop génériques
    # contenus dans l'extrait ne doivent pas transformer tout article en "coopération".
    head = title.casefold()
    priority = [
        ("inclusion", r"ulis|différenci|inclusi|\bdys\b|handicap"),
        ("cooperation", r"classe puzzle|coopér|collabor|travail en groupe"),
        ("assessment", r"évalua|remédi|autoévalua"),
        ("astronomy", r"astronom|cosmos|univers|étoile|galaxi|nucléosynth"),
        ("matter", r"physique.chimie|chimie|dissol|dissou|solubil|molécul|cristal|atome"),
        ("energy", r"énergie|climat|électri|thermi|rayonnement"),
        ("sound", r"acoustique|musique|fréquence|sonore"),
        ("math", r"mathém|maths|statisti|proportion|calcul|géométrie|graphi"),
        ("ia", r"intelligence artificielle|\bia\b|algorith|donn[ée]|biais|modèle|numérique"),
        ("cooperation", r"coopér|collabor|collectif|classe puzzle|travail en groupe|équipe|ensemble")
    ]
    for name, pattern in priority:
        if re.search(pattern, head):
            return name
    t = (title + " " + excerpt[:380]).casefold()
    patterns = [
        ("cooperation", r"coopér|collabor|collectif|classe puzzle|travail en groupe|équipe|ensemble"),
        ("inclusion", r"ulis|différenci|inclusi|dys|handicap|besoins éducatifs"),
        ("assessment", r"évalua|autoévalua|feedback|remédi|erreur|quiz|qcm"),
        ("ia", r"intelligence artificielle|\bia\b|algorith|donn[ée]|biais|modélis|esprit critique|fiabilit"),
        ("astronomy", r"astronom|cosmos|univers|étoile|planète|solaire|galaxi|nucléosynth"),
        ("matter", r"chimie|matière|dissol|dissou|solubil|molécul|atome|cristal|substance|réaction chimique"),
        ("energy", r"énergie|électri|climat|thermi|rayonnement|renouvelable"),
        ("sound", r"acoustique|musique|fréquence|sonore|spectre"),
        ("math", r"mathém|maths|statisti|proportion|mesure|calcul|géométrie|graphi|puissance de dix")
    ]
    for name, pattern in patterns:
        if re.search(pattern, t):
            return name
    return "default"

def teaching_ideas(item):
    theme = teaching_theme(item["title"], item.get("excerpt", ""))
    labels = {"cooperation":"Coopération", "inclusion":"Différenciation", "assessment":"Évaluation", "ia":"Données et esprit critique", "astronomy":"Univers", "matter":"Matière", "energy":"Énergie", "sound":"Son", "math":"Outils mathématiques", "default":"Démarche scientifique"}
    a, b = SCENARIOS[theme]
    def to_dict(row):
        level, duration, objective, activity, trace = row
        return {"level":level, "duration":duration, "objective":objective, "activity":activity, "trace":trace}
    return labels[theme], {"college":to_dict(a), "premiere":to_dict(b)}


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


def to_output(source, summary, interest=""):
    label, applications = teaching_ideas(source)
    published = source["published_utc"]
    fresh = published >= FRESH_SINCE
    return {
        "title": source["title"], "url": source["url"], "source": source["source"],
        "date": published.astimezone(PARIS).date().isoformat(),
        "category": source["category"], "summary": summary,
        "interest": interest or f"Deux idées d’activités proposées autour du thème « {label.lower()} ».",
        "theme": label, "fresh": fresh, "applications": applications
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


def previous_candidates():
    """Relit l'édition en ligne : un workflow GitHub ne conserve pas ses fichiers temporaires."""
    old = None
    site_url = os.getenv("SITE_URL", "").strip()
    if site_url.startswith("https://"):
        try:
            published = requests.get(site_url.rstrip("/") + "/veille.json", timeout=(5, 12),
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            published.raise_for_status()
            old = published.json()
            print("[OK] Édition publiée précédente récupérée pour assurer la continuité.")
        except (requests.RequestException, ValueError) as e:
            print(f"[INFO] Édition précédente en ligne indisponible : {str(e)[:120]}")
    if old is None:
        try:
            old = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
    if not isinstance(old, dict):
        return []
    prior = []
    for a in old.get("articles", []):
        try:
            dt = datetime.fromisoformat(a["date"]).replace(tzinfo=PARIS).astimezone(timezone.utc)
            if dt < SINCE or dt > NOW:
                continue
            url = normalize_url(a.get("url"))
            if not url:
                continue
            item = {"title":a["title"], "url":url, "source":a.get("source", "Source conservée"),
                    "excerpt":a.get("summary", ""), "published_utc":dt,
                    "category":a.get("category", "Pédagogie"),
                    "prior_summary":a.get("summary", "")}
            item["score"] = score_for(item)
            if item["score"] >= 5:
                prior.append(item)
        except (KeyError, ValueError, TypeError):
            continue
    return prior


def main():
    print(f"Veille du {NOW.astimezone(PARIS):%Y-%m-%d %H:%M %Z}. Nouveautés : {WINDOW_HOURS} h ; complément : {LOOKBACK_DAYS} jours.")
    articles, errors = previous_candidates(), []
    with ThreadPoolExecutor(max_workers=7) as pool:
        jobs = {pool.submit(fetch_feed, feed): feed[0] for feed in FEEDS}
        for future in as_completed(jobs):
            label = jobs[future]
            try:
                found = future.result()
                articles.extend(found)
                recent = sum(1 for x in found if x["published_utc"] >= FRESH_SINCE)
                print(f"[OK] {label} : {recent} article(s) sous 48 h, {len(found)} sur 7 jours")
            except Exception as e:
                errors.append(label)
                print(f"[ATTENTION] {label} inaccessible : {str(e)[:220]}")
    unique = {}
    for a in articles:
        key = a["url"]
        if key not in unique or (not a.get("prior_summary") and unique[key].get("prior_summary")) or a["score"] > unique[key]["score"]:
            unique[key] = a
    fresh = sorted((x for x in unique.values() if x["published_utc"] >= FRESH_SINCE), key=lambda x:(x["score"],x["published_utc"]), reverse=True)
    older = sorted((x for x in unique.values() if x["published_utc"] < FRESH_SINCE), key=lambda x:(x["score"],x["published_utc"]), reverse=True)
    # Deux temps : nouveautés prioritaires, puis articles complémentaires si moins de cinq.
    candidates = fresh + older
    assisted = ai_selection(candidates) if os.getenv("OPENAI_API_KEY") else None
    if assisted:
        selected = [(a, summary) for a, summary, _ in assisted]
        mode_description = "Résumés assistés par IA à partir d’extraits RSS ; activités proposées séparément."
    else:
        selected = [(a, a.get("prior_summary") or fallback_summary(a["excerpt"], a["title"])) for a in pick_ranked(candidates)]
        mode_description = "Résumés issus des extraits RSS ; idées de séances proposées à adapter."
    chosen = [to_output(a, summary) for a, summary in selected]
    recent_count = sum(1 for a in chosen if a["fresh"])
    message = (f"{recent_count} publication(s) des dernières 48 h ; "
               f"{len(chosen)-recent_count} ressource(s) complémentaire(s) des 7 derniers jours. "
               + mode_description)
    if errors:
        message += f" {len(errors)} flux momentanément inaccessible(s)."
    if not chosen:
        message = "Aucun article daté et suffisamment pertinent sur sept jours. Les ressources institutionnelles restent disponibles ci-dessous."
    data = {
        "generated_at": NOW.astimezone(PARIS).isoformat(timespec="minutes"),
        "mode": "automatic" if chosen else ("error" if len(errors) == len(FEEDS) else "empty"),
        "window_hours": WINDOW_HOURS, "lookback_days": LOOKBACK_DAYS,
        "fresh_count": recent_count, "source_errors": errors,
        "message": message, "articles": chosen
    }
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_rss(data)
    print(f"[TERMINÉ] {len(chosen)} ressource(s) écrite(s) ; dont {recent_count} sous 48 h")


if __name__ == "__main__":
    main()
