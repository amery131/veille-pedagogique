# Veille pédagogique : widget HTML pour Vivaldi

**Un tableau de bord compact, en français, pour le widget « Page web » de Vivaldi.**

## Les fichiers

- `index.html` : le module au format 394 × 380 px (responsive), avec navigation par rubriques, résumés, intérêts pédagogiques et liens vers les sources.
- `veille.json` : aperçu initial avec **5 articles anciens et vérifiés, explicitement marqués comme démonstration**.
- `update_veille.py` : collecte quotidienne des RSS, filtre sur les **48 heures écoulées**, retient **au maximum cinq ressources** et crée `veille.json` et `veille.xml`.
- `veille.xml` : flux RSS du bulletin commenté (créé lors d'une exécution automatique).
- `.github/workflows/publier-veille.yml` : programmation et publication automatique sur GitHub Pages.
- `requirements.txt` : dépendances du script Python.

## 1. Prévisualiser immédiatement

Double-cliquez sur `index.html` après extraction du ZIP. La page s'ouvre dans Vivaldi. Les cinq articles affichés à cette étape ne constituent **pas** la veille du 25 octobre 2026 : ce sont des exemples datés, avec leurs véritables références.

## 2. Mise à jour quotidienne, sans ordinateur allumé

1. Créez un compte GitHub si vous n'en avez pas, puis un **dépôt public** appelé `veille-pedagogique` (la formule gratuite suffit pour ce projet).
2. Décompressez le ZIP et déposez **tout son contenu**, y compris le dossier caché `.github`, à la racine du dépôt. Sur GitHub, utilisez **Add file > Upload files** ou un outil Git qui conserve les sous-dossiers.
3. Ouvrez **Settings > Pages** et choisissez **Source : GitHub Actions** (et non « Deploy from a branch »).
4. Ouvrez **Actions > Publier la veille pédagogique > Run workflow** une première fois. Autorisez les Actions si GitHub le demande.
5. Après publication, votre adresse est normalement :

   `https://VOTRE_IDENTIFIANT.github.io/veille-pedagogique/`

   Si votre dépôt porte un autre nom, adaptez la fin de l'URL. Vérifiez l'adresse exacte sous **Settings > Pages**.
6. Dans Vivaldi, ouvrez **un nouvel onglet > Widgets > Ajouter un widget > Page web** puis collez cette adresse.

Le service GitHub Actions effectue une collecte **tous les jours vers 19 h 10, heure de Paris**, été comme hiver. L'exécution peut être décalée par GitHub. Le widget recharge les nouvelles données lorsqu'il est ouvert et les revérifie toutes les trente minutes. **Votre ordinateur peut être éteint.**

### Ce qui est automatique sans abonnement payant

- Lecture des 7 flux RSS publics : Édubase (physique-chimie, mathématiques), Cahiers pédagogiques, Café pédagogique, Éduveille/IFÉ, Ludomag, Interstices.
- Tri des articles pertinents publiés dans les dernières 48 heures. **Un article plus ancien n'est pas présenté comme nouveau.**
- Conservation des dates, titres, noms des sources et vrais liens originaux.
- Résumé basé sur le texte d'aperçu du RSS et suggestion pédagogique formulée selon le thème.
- Publication du module et d'un flux RSS `veille.xml`.

**Limite :** le résumé gratuit repose sur l'extrait du flux, sans lecture approfondie de l'article. La sélection est algorithmique et peut manquer une ressource importante ou proposer une piste pédagogique trop générique.

### Option : commentaires plus fins avec l'API OpenAI (facultative et payante à l'usage)

Dans le dépôt, allez dans **Settings > Secrets and variables > Actions > New repository secret**. Nommez le secret `OPENAI_API_KEY` et collez votre clé d'API (à créer et facturer séparément de l'abonnement ChatGPT). Ne mettez **jamais** cette clé dans les fichiers HTML, Python ou JSON accessibles au public.

Le script utilise alors un modèle d'IA pour sélectionner et résumer les extraits et rédiger les pistes pédagogiques. Il ne peut pas consulter automatiquement vos conversations ou tâches programmées dans ChatGPT : **le bulletin généré sur GitHub est une veille parallèle utilisant les mêmes critères, pas une copie de votre rapport ChatGPT quotidien**.

Si l'API est indisponible, la version gratuite continue de fonctionner. Une lecture critique des résumés reste nécessaire.

## 3. Abonnement RSS facultatif

Une fois le site publié, le flux commenté est disponible sur :

`https://VOTRE_IDENTIFIANT.github.io/veille-pedagogique/veille.xml`

Vous pouvez l'ajouter au lecteur RSS intégré de Vivaldi pour conserver les éditions comme articles RSS.

## 4. Points de vigilance

- Le dépôt et les résultats sont publics sur GitHub Pages. N'y placez aucune donnée d'élève ni information personnelle.
- Sans article récent suffisamment pertinent, le module affichera **moins de cinq ressources**, voire aucune, plutôt que d'inventer des informations ou d'utiliser d'anciens articles.
- Un flux temporairement indisponible est ignoré ; si tous les flux échouent, le module signale l'échec.
- Les actions planifiées peuvent subir un retard, notamment lorsque GitHub est très sollicité, ou être désactivées après une longue inactivité d'un dépôt public. Vous pouvez relancer manuellement une édition avec **Run workflow**.
- Le code ne télécharge que le texte d'aperçu RSS et non les articles intégraux. Chaque titre ouvre la source d'origine.
- La page utilise les liens externes en `noopener noreferrer`, échappe les textes insérés dans le HTML, et n'utilise aucune dépendance JavaScript externe.

## 5. Modifier les sources et la sélection

Dans `update_veille.py` : `FEEDS` contient les abonnements à ajuster. `WINDOW_HOURS = 48` définit la fenêtre, le nombre maximum est réglé à 5 dans `pick_ranked()`. Les catégories sont « Physique-chimie », « Mathématiques », « Pédagogie » et « IA et numérique ».

---

**Solution prévue pour :** physique-chimie au collège, enseignement scientifique en première, mathématiques et pédagogie en France.
