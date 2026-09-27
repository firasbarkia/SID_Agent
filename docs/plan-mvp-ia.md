# Plan du MVP IA — SID Agent

Statut : proposition de réalisation, sans implémentation fonctionnelle.

## 1. Objectif et point de départ

Construire un assistant de recrutement pour les candidats et les entreprises, avec validation humaine obligatoire avant toute publication, candidature ou prise de contact. Les recherches, analyses et générations privées peuvent être exécutées sans confirmation supplémentaire.

Le projet actuel contient uniquement une application FastAPI, les routes `/` et `/health`, un environnement Python et un verrouillage des dépendances avec uv. L'authentification, les données métier, le stockage documentaire et les services IA restent à construire ou à connecter.

Décision structurante à confirmer : ce service fournit-il uniquement l'IA à une plateforme existante, ou constitue-t-il aussi son backend métier ? Les étapes IA ci-dessous sont communes aux deux cas. Si aucune plateforme n'existe, prévoir un lot préalable pour les comptes, les organisations, les profils, les offres et les candidatures. Son interface utilisateur et son estimation sont distinctes de ce plan backend IA.

Hypothèses de travail : contenu français et anglais ; offres et informations d'entreprise fournies par la plateforme ; candidats débutants et stagiaires évaluables à partir de leurs projets ; aucun scraping d'entreprise nécessaire au MVP. Le fournisseur de modèles, le lieu d'hébergement et le budget restent à choisir avant les appels sur des données réelles.

## 2. Parcours et périmètre

| Fonctionnalité | Traitement prévu | Résultat présenté à l'utilisateur | Validation |
|---|---|---|---|
| Import de CV | PDF → texte → extraction structurée → normalisation | Brouillon de profil et source de chaque information | Correction et acceptation des champs ; publication séparée |
| Dossier personnalisé | Profil validé + offre + fiche entreprise → analyse des critères | Lettre modifiable, conseils CV, exigences couvertes et manquantes | Confirmation du dossier exact avant envoi |
| Évaluation du profil | Règles de complétude + analyse du contenu | Score de complétude, points forts et trois actions prioritaires | Aucune modification automatique du profil |
| Recherche d'offres | Phrase → filtres structurés + recherche lexicale et sémantique | Filtres modifiables, offres réelles et motifs de pertinence | Recherche immédiate ; candidature confirmée séparément |
| Sourcing entreprise | Requête libre ou fiche de poste → critères → recherche | Profils visibles et pertinents, preuves et informations manquantes | Critères éditables ; aucun contact automatique |
| Matching et top 5 | Comparaison des critères avec les preuves des profils | Jusqu'à cinq profils avec indice de compatibilité détaillé | Choix du recruteur ; aucune décision automatique de recrutement |
| Présentation anonymisée | Projection des données avant classement et affichage | Profils sans identité directe ni coordonnées | Option entreprise ; visibilité toujours soumise au consentement du candidat |

Le premier MVP accepte les PDF contenant du texte. Les fichiers scannés sont détectés et accompagnés d'une demande de PDF texte ou de saisie manuelle. L'OCR, l'export d'un CV entièrement remis en page et la recherche web d'informations d'entreprise viennent ensuite, sauf si les documents pilotes démontrent qu'ils sont indispensables.

## 3. Architecture proposée

Un monolithe FastAPI organisé par domaine et un worker pour les tâches longues suffisent au MVP. L'« agent » est un orchestrateur de workflows explicites, avec des outils autorisés par étape.

- **FastAPI et Pydantic** : API, validation des entrées, schémas de sortie IA et contrôle des autorisations.
- **PostgreSQL, SQLAlchemy et Alembic** : données métier nécessaires, versions, validations, tâches et migrations.
- **pgvector et recherche textuelle PostgreSQL** : recherche hybride dans la même base, avec filtres explicites. Commencer par une recherche vectorielle exacte à faible volume ; ajouter un index approximatif après mesure.
- **Stockage privé compatible S3** : CV et documents, avec accès temporaires et contrôlés.
- **Worker Celery et Redis** : extraction, génération et indexation ; état durable des tâches dans PostgreSQL, reprises idempotentes et délais maximum.
- **Adaptateur IA** : interfaces `extract_structured`, `generate_text` et `embed`, indépendantes du fournisseur. Chaque sortie est validée avant stockage ou affichage.
- **Adaptateurs métier** : accès aux profils, entreprises, offres et soumissions. Ils ciblent la plateforme existante ou les modules métier locaux suivant la décision d'intégration.

Le modèle ne dispose pas d'un outil permettant de publier ou d'envoyer directement. Seul le code applicatif peut déclencher ces actions après contrôle de la confirmation.

Organisation indicative :

```text
app/
  main.py
  api/                 # routes et dépendances d'authentification
  core/                # configuration et sécurité
  db/                  # sessions, modèles et migrations
  schemas/             # contrats métier et sorties IA
  services/
    cv_import.py
    profile_evaluation.py
    application_drafts.py
    search.py
    matching.py
    approvals.py
  integrations/        # fournisseur IA, stockage, plateforme métier
  workers/             # exécution asynchrone
tests/
```

## 4. Données et traçabilité

Entités principales :

- `CandidateProfileVersion` : expériences, projets, formations, compétences, langues, préférences et visibilité. Une version validée reste distincte d'un brouillon importé.
- `Company` et `JobVersion` : informations sourcées sur l'entreprise, description, critères obligatoires, critères souhaitables et préférences. Une fiche de poste analysée par l'IA reste éditable par le recruteur.
- `Document` et `ExtractionDraft` : propriétaire, fichier, texte extrait, valeurs proposées, pages ou passages sources, champs absents ou ambigus.
- `ApplicationDraftVersion` : profil, offre et contexte entreprise utilisés, lettre et recommandations CV.
- `MatchResult` : versions comparées, critères, sous-scores, preuves, inconnues et version du barème.
- `Approval` : utilisateur, action autorisée, version et empreinte du contenu, destinataire, date et état de consommation.
- `Submission` : dossier confirmé, destination, clé d'idempotence et résultat d'envoi.
- `AITask` et `AuditEvent` : états de traitement, modèle, version de prompt, durée, coût estimé et événements métier, sans journaliser le CV intégral par défaut.

Une modification de profil ou d'offre invalide les caches et résultats dérivés concernés. L'indexation utilise les versions validées et actuellement visibles, avec filtrage des droits avant la recherche et contrôle à nouveau avant restitution.

## 5. Validation humaine : invariant serveur

Pour les actions de publication et d'envoi :

```text
BROUILLON → À_VALIDER → APPROUVÉ → EN_COURS → ENVOYÉ / PUBLIÉ
                ↑          |
                └── modification du contenu
```

1. L'utilisateur peut générer et modifier librement un brouillon privé.
2. L'écran de confirmation montre le contenu final, les pièces jointes et le destinataire.
3. La confirmation crée une autorisation portant sur une version immuable et sur une action précise.
4. Le serveur vérifie l'identité, les droits, la version, le destinataire et l'état de l'autorisation avant l'action.
5. Toute modification impose une nouvelle validation. L'acceptation d'un profil extrait n'autorise pas implicitement sa publication, son sourcing ou une candidature.
6. Une clé d'idempotence et une contrainte d'unicité empêchent le double envoi. Une outbox transactionnelle conserve les demandes d'envoi à exécuter après validation.
7. En cas de réponse réseau ambiguë, réconcilier l'état avec la plateforme destinataire avant une relance. L'absence de garantie d'idempotence externe doit être traitée explicitement par l'adaptateur.

## 6. Traitements IA

### 6.1 Import de CV

Vérifier le type réel du fichier, limiter sa taille et son nombre de pages, puis extraire le texte avec ses références de page. Les PDF illisibles, chiffrés ou sans texte exploitable déclenchent un retour explicite.

Demander une extraction conforme à un schéma : coordonnées, expériences, formations, projets, compétences et langues. Une donnée absente reste `null` ou absente ; le modèle n'invente ni diplôme, ni date, ni niveau de compétence. Chaque proposition comporte sa provenance et un état « extrait », « ambigu » ou « à confirmer ». Une probabilité produite par le modèle ne constitue pas une confiance calibrée.

Présenter les différences avec le profil existant. Le candidat choisit les champs à intégrer pour éviter tout écrasement silencieux.

### 6.2 Dossier sur mesure

Construire d'abord une matrice « exigence de l'offre → preuve du profil → lacune éventuelle ». Générer ensuite la lettre uniquement à partir de cette matrice et des informations d'entreprise disponibles. Si le contexte entreprise manque, produire une personnalisation limitée à l'offre et le signaler.

Les conseils CV proposent de réordonner ou reformuler des expériences réellement présentes. Ils ne transforment jamais une compétence souhaitée en compétence possédée. Les expériences et projets utilisés restent identifiables dans le résultat.

### 6.3 Évaluation du profil

Calculer le score de complétude par règles versionnées, de façon reproductible. Barème initial proposé : compétences 20 points, expériences ou projets 30, formation 15, préférences de recherche 15, présentation 10, moyen de contact privé valide 10. Une section déclarée non applicable est traitée selon une règle documentée ; les profils de stagiaires peuvent atteindre la complétude via leurs projets.

L'IA explique les points forts et suggère des améliorations à partir des données disponibles. Ce score mesure le renseignement du profil, pas la valeur professionnelle du candidat. Il n'entre pas dans le classement de recrutement.

### 6.4 Recherche conversationnelle

Exemple : « Je cherche un stage PFE en développement web à Sfax dans une startup ».

L'analyse produit des critères comme `type=stage`, `stage_type=PFE`, `city=Sfax`, `company_type=startup` et `query=développement web`. Les attributs absents des données ne sont pas inventés. Les critères interprétés sont affichés et modifiables.

Appliquer les droits et les filtres explicites, combiner les résultats lexicaux et vectoriels, puis restituer des offres réellement stockées. Une contrainte explicite n'est jamais assouplie silencieusement ; si aucun résultat ne correspond, proposer à l'utilisateur les filtres à élargir. Les messages de suivi peuvent modifier les critères d'une recherche existante.

### 6.5 Matching et explications

Séparer la récupération de profils pertinents du calcul du score final. Les embeddings aident à retrouver des compétences proches ; leur similarité brute n'est pas un pourcentage de compatibilité.

Maintenir un vocabulaire de compétences avec alias et relations : « ReactJS » et « React » sont des alias ; React est une compétence frontend, mais la mention générique « frontend » ne prouve pas la maîtrise de React.

Barème initial proposé, à calibrer sur des exemples annotés : compétences requises 50 %, expériences ou projets pertinents 30 %, compétences souhaitables 20 %. Les contraintes objectives explicitement confirmées par le recruteur sont traitées à part ; les données manquantes restent « à vérifier ». Un résultat IA incertain n'élimine pas silencieusement un profil.

Calculer chaque contribution à partir d'éléments traçables. Renvoyer l'indice sur 100, le détail par critère, la couverture des informations et les points à vérifier. Si les informations sont insuffisantes, afficher « compatibilité à vérifier » plutôt qu'un score artificiellement précis. L'indice ne représente pas une probabilité de réussite ou d'embauche.

La justification est construite à partir des critères réellement utilisés et de leurs preuves ; une génération rédactionnelle éventuelle ne modifie ni le score ni les faits. Renvoyer jusqu'à cinq profils, sans compléter artificiellement une liste insuffisante.

### 6.6 Présentation anonymisée

Retirer au minimum le nom, la photo, les coordonnées, l'adresse précise, la date de naissance et les liens identifiants. Vérifier également les textes libres et les pièces jointes. Les coordonnées restent séparées des données utilisées pour le classement.

Le scoring utilise uniquement les critères professionnels retenus, même lorsque l'affichage anonymisé est désactivé. La suppression de l'identité réduit certaines sources de biais ; elle ne garantit pas une sélection impartiale. Tester notamment qu'un changement d'identité à compétences identiques ne modifie pas le score.

## 7. Contrats API proposés

Préfixe : `/api/v1`. Les routes supposent une identité authentifiée et des autorisations candidat ou entreprise.

| Méthode et route | Rôle |
|---|---|
| `POST /cv-imports` | Déposer un PDF et lancer l'extraction |
| `GET /tasks/{id}` | Consulter l'état d'une tâche et son résultat autorisé |
| `GET /cv-imports/{id}` | Lire le brouillon et les références sources |
| `PATCH /cv-imports/{id}` | Corriger les champs extraits |
| `POST /cv-imports/{id}/accept` | Intégrer explicitement la version corrigée au profil privé |
| `POST /profiles/me/evaluations` | Évaluer une version du profil |
| `POST /application-drafts` | Générer un dossier pour une offre |
| `GET /application-drafts/{id}` | Lire le dossier et ses sources |
| `PATCH /application-drafts/{id}` | Créer une version corrigée du dossier |
| `POST /approvals` | Confirmer une action et une version précises |
| `POST /application-drafts/{id}/submit` | Soumettre le dossier avec une approbation valide |
| `POST /profiles/me/publish` | Publier une version avec approbation et paramètres de visibilité |
| `POST /jobs/search` | Chercher des offres par texte et filtres |
| `POST /candidates/search` | Sourcer des candidats visibles avec une requête ou une offre |
| `POST /jobs/{id}/matches` | Calculer le classement pour une version d'offre |
| `GET /jobs/{id}/matches` | Consulter le classement, limité à cinq résultats par défaut |

Les opérations longues répondent `202 Accepted` avec un identifiant de tâche et une URL d'état. États : `queued`, `running`, `succeeded`, `failed`, `cancelled`. Les consultations vérifient les droits sur la tâche et sur son résultat. Les écritures portent un numéro de version pour détecter les modifications concurrentes.

Les routes CRUD des comptes, offres, organisations et candidatures seront spécifiées dans le lot métier si elles ne sont pas déjà fournies par la plateforme.

## 8. Ordre de réalisation et critères d'acceptation

| Lot | Livrable | Preuve attendue |
|---|---|---|
| 0 — Cadrage et jeux d'essai | Contrats d'intégration, critères de visibilité, exemples anonymisés, choix fournisseur | Scénarios candidat et recruteur validés ; budget d'appels défini |
| 1 — Socle | Authentification ou intégration, base, stockage, worker, versions et validations | Impossible de consulter les CV d'un autre utilisateur ou d'envoyer sans confirmation |
| 2 — CV et évaluation | Import, édition, acceptation, complétude et conseils | CV → profil validé ; champs absents préservés ; scan détecté ; projets valorisés |
| 3 — Dossier et soumission | Matrice de preuves, lettre, conseils CV, approbation et envoi | Une modification invalide l'approbation ; une relance ne crée pas deux candidatures |
| 4 — Recherche d'offres | Interprétation des requêtes, filtres et recherche hybride | Requête PFE/Sfax correcte ; contraintes respectées ; zéro résultat traité clairement |
| 5 — Sourcing et top 5 | Recherche candidats, critères, score détaillé et explications | Alias reconnus ; compétences proches non confondues ; chaque justification sourcée |
| 6 — Pilote | Mesures de qualité, coût, latence et corrections | Validation humaine des résultats sur le corpus pilote avant ouverture |
| Option — Anonymisation | Projection et tests de fuite d'identité | Identité masquée dans les réponses, textes libres et documents accessibles |

La première démonstration complète doit suivre un seul parcours : importer un CV, valider le profil, choisir une offre, générer et corriger une lettre, puis confirmer une candidature. Le sourcing réutilise ensuite les mêmes profils validés et la même matrice de critères.

Une estimation calendaire fiable nécessite de connaître l'équipe, l'existence du frontend et des services métier, le volume de documents et la qualité des données. Chiffrer chaque lot après le cadrage ; éviter d'inclure implicitement la construction d'une plateforme entière dans le seul lot IA.

## 9. Vérification et exploitation

Constituer un corpus pilote autorisé ou synthétique : CV français/anglais, débutants, profils expérimentés, documents incomplets, scans, offres ambiguës et couples offre-profil annotés par un recruteur.

Mesurer séparément : exactitude de l'extraction par champ, respect des filtres, pertinence du top 5, fidélité des explications, affirmations sans preuve dans les lettres, coût par tâche et latence au 95e percentile. Fixer les seuils avec le corpus et le budget avant l'acceptation du pilote.

Les contrôles bloquants couvrent : publication sans approbation, validation d'une ancienne version, accès entre organisations, CV contenant des instructions malveillantes, double envoi, panne du fournisseur et perte du worker. Les contenus importés sont des données non fiables ; ils ne peuvent ni changer les consignes de l'agent ni déclencher un outil externe.

Versionner modèles, prompts et barèmes. Limiter la concurrence et le nombre de tentatives ; conserver les brouillons si le fournisseur échoue. Prévoir la suppression du document, des données dérivées et des embeddings selon la politique de conservation définie. Les quotas et délais doivent produire des messages exploitables par l'utilisateur.

## 10. Décisions ouvertes

1. Service IA intégré ou backend métier complet ; source des comptes, offres et profils.
2. Fournisseur IA, hébergement, traitement des CV et budget mensuel.
3. Langues initiales et importance des CV scannés ; l'arabe doit être explicitement ajouté au corpus s'il entre dans le périmètre.
4. Canaux de soumission et de contact ; garanties d'idempotence de la plateforme destinataire.
5. Règles de visibilité des candidats et moment d'activation de l'affichage anonymisé.
6. Volumétrie, objectifs de latence et critères d'acceptation métier du pilote.

## Références techniques

- [PostgreSQL — recherche textuelle](https://www.postgresql.org/docs/current/textsearch.html) : recherche lexicale et classement.
- [pgvector — documentation officielle](https://github.com/pgvector/pgvector) : recherche vectorielle, filtrage et combinaison avec la recherche textuelle PostgreSQL.
- [NIST — caractéristiques d'une IA digne de confiance](https://airc.nist.gov/airmf-resources/airmf/3-sec-characteristics/) : distinction entre explicabilité, validité et gestion des biais. L'anonymisation n'est pas une preuve d'impartialité.
