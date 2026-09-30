# Plan du MVP IA — SID Agent

Statut : plan fonctionnel du MVP ; première tranche API/IA commencée le 30 septembre 2026. Voir [les phases d'implémentation et tests](implementation-phases.md) pour les capacités livrées et restantes.

Contraintes confirmées : MongoDB autohébergé est la base principale et la demande future est inconnue. Le plan doit permettre de dimensionner séparément l'API, les traitements IA et la recherche. Recommandation d'architecture : MongoDB comme source de vérité et Qdrant comme index de recherche reconstructible. Ce choix propose un second service à exploiter et une synchronisation explicite ; aucune installation n'est encore effectuée.

## 1. Objectif et point de départ

Construire un assistant de recrutement pour les candidats et les entreprises, avec validation humaine obligatoire avant toute publication, candidature ou prise de contact. Les recherches, analyses et générations privées peuvent être exécutées sans confirmation supplémentaire.

Le projet contient désormais une première tranche FastAPI : configuration, authentification interne par clé de service, aperçu de lettre non persisté, adaptateurs Groq/Gemini et circuit breakers par fournisseur. Les données métier, l'identité des utilisateurs, le stockage documentaire et les workflows persistants restent à construire ou à connecter. La clé de service ne remplace pas l'autorisation candidat/entreprise.

Le service IA s'appuie sur la base MongoDB principale et réutilise les identifiants et schémas métier existants. Le cadrage doit identifier les modules déjà disponibles pour les comptes, organisations, profils, offres et candidatures, ainsi que le service propriétaire de leurs écritures. Construire uniquement les éléments manquants ; éviter de créer une deuxième copie faisant autorité sur ces données. Le frontend reste un périmètre distinct de ce plan backend IA.

Hypothèses de travail : contenu français et anglais ; offres et informations d'entreprise fournies par la plateforme ; candidats débutants et stagiaires évaluables à partir de leurs projets ; aucun scraping d'entreprise nécessaire au MVP. Génération : Groq/Llama 3.1 en primaire, Gemini en repli. L'accès gratuit au modèle demandé doit être vérifié dans le compte Groq ; le modèle d'embedding, le lieu d'hébergement et le budget restent à préciser.

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
- **MongoDB et PyMongo Async** : accès asynchrone à la base principale, collections IA, versions, validations et tâches. Pydantic définit les contrats applicatifs ; les validateurs de collections, index et scripts de migration versionnés protègent les données persistées.
- **Qdrant et un adaptateur `SearchService`** : index vectoriel indépendant pour les candidats et les offres, avec identifiants métier, versions et métadonnées de filtrage. Les dossiers, validations et candidatures restent dans MongoDB. La recherche lexicale peut initialement utiliser les index texte MongoDB ; sa combinaison avec la recherche sémantique est évaluée sur le corpus français/anglais.
- **Stockage documentaire existant** : réutiliser le stockage privé de la plateforme. Si aucun stockage de fichiers n'existe et que tous les documents doivent rester dans MongoDB, utiliser GridFS ; les métadonnées et droits restent dans les collections métier.
- **Worker Python et collection `ai_tasks`** : extraction, génération et indexation, sans broker supplémentaire au MVP. Réservation atomique des tâches, bail renouvelable, reprises idempotentes, nombre de tentatives limité et délais maximum. Le worker est un processus distinct du serveur FastAPI.
- **Adaptateur IA** : gateway de génération Groq → Gemini avec circuit breaker par fournisseur, délais bornés et limitation de concurrence. La première API produit un brouillon texte non persisté ; extraction structurée et embeddings sont des capacités à ajouter. Le modèle d'embedding restera cohérent entre requêtes et index Qdrant, sans repli automatique vers un espace vectoriel incompatible.
- **Adaptateurs métier** : accès aux profils, entreprises, offres et soumissions. Ils ciblent la plateforme existante ou les modules métier locaux suivant la décision d'intégration.

Le modèle ne dispose pas d'un outil permettant de publier ou d'envoyer directement. Seul le code applicatif peut déclencher ces actions après contrôle de la confirmation.

**Choix de recherche et croissance :** la séparation avec Qdrant est recommandée pour faire évoluer la recherche indépendamment de la version et des ressources du MongoDB existant. Elle ne prouve pas une supériorité de performance : comparer la qualité et la latence sur des données représentatives. MongoDB Vector Search avec des processus `mongot` séparés demeure une alternative capable de dimensionnement indépendant, sous réserve de compatibilité et de topologie.

### Dimensionnement progressif

- Développement : un processus API, un worker et un nœud Qdrant avec stockage persistant permettent de valider le parcours complet. Ce déploiement ne constitue pas une configuration hautement disponible.
- Avant production : définir un objectif de disponibilité et les délais de reprise acceptables ; configurer sauvegardes, restauration et, si nécessaire, réplication sur plusieurs nœuds. Prévoir un test de perte de nœud et une stratégie de reconstruction de l'index.
- Croissance : répliquer les instances FastAPI sans état local, augmenter les workers selon la file d'attente et les quotas du fournisseur IA, puis répartir les shards et réplicas Qdrant selon les mesures. Isoler les ressources de recherche de celles de MongoDB lorsque la contention l'exige.
- Mesures de capacité : nombre de vecteurs et dimensions, volume de mises à jour, requêtes simultanées, latence p95, qualité du top 5, RAM, disque, retard d'indexation, âge des tâches et coût des appels IA. Tester des charges progressives, par exemple 1×, 5× et 10× une référence pilote mesurée ; ces facteurs sont des scénarios d'essai, pas des prévisions de trafic.
- Limites de charge : files bornées, limites par organisation, reprise avec temporisation, concurrence maximale et pagination. Séparer les tâches interactives des réindexations massives pour préserver les temps de réponse. Mesurer aussi la charge de réservation des tâches sur MongoDB avant d'ajouter davantage de workers.

Qdrant autohébergé supporte la distribution, mais ajouter un nœud ne répartit pas automatiquement les données ni ne configure leur réplication. Le nombre de shards et leur placement doivent être planifiés ; un changement de partitionnement peut nécessiter une nouvelle collection. Prévoir une procédure de reconstruction et de bascule, plutôt que promettre une élasticité automatique.

Organisation indicative :

```text
app/
  main.py
  api/                 # routes et dépendances d'authentification
  core/                # configuration et sécurité
  db/                  # client MongoDB, collections, index et migrations
  repositories/        # accès aux collections et écritures conditionnelles
  schemas/             # contrats métier et sorties IA
  services/
    cv_import.py
    profile_evaluation.py
    application_drafts.py
    search.py
    matching.py
    approvals.py
  integrations/        # fournisseur IA, Qdrant, stockage, plateforme métier
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

### Organisation MongoDB

Les noms suivants sont indicatifs et seront adaptés aux collections existantes :

| Collections | Responsabilité |
|---|---|
| Collections métier existantes | Profils, entreprises, offres et candidatures ; sources de vérité conservées |
| `ai_cv_imports`, `ai_profile_evaluations` | Brouillons d'extraction, références sources et diagnostics |
| `ai_application_drafts`, `ai_application_versions` | État courant du dossier et versions immuables à valider |
| `ai_search_documents` | Texte normalisé, version et état d'indexation ; référence au point Qdrant et métadonnées de visibilité |
| `ai_match_results` | Scores, preuves et versions de profil, d'offre et de barème |
| `ai_approvals`, `ai_submissions`, `ai_outbox` | Confirmations, soumissions idempotentes et demandes d'envoi |
| `ai_tasks`, `ai_audit_events` | Exécution asynchrone et événements d'audit |

Les projections de recherche référencent les identifiants d'origine ; elles ne remplacent pas les profils et offres métier. Chaque projection conserve `source_id`, `source_version`, `embedding_model`, `embedding_dimensions`, `content_hash` et les droits nécessaires aux filtres. Une tâche tardive ne peut pas remplacer une projection plus récente. Une réindexation doit être prévue lors d'un changement de modèle ou de dimension des embeddings.

### Synchronisation MongoDB → Qdrant

Une mise à jour métier et un événement d'indexation sont enregistrés dans la même transaction MongoDB par le service propriétaire. Le worker traite cet événement après commit, relit les données autorisées, calcule l'embedding et écrit le point Qdrant. Il marque l'événement traité après acquittement ; les reprises sont idempotentes. Il n'y a pas de transaction distribuée entre les deux bases.

Les points possèdent un identifiant déterministe dérivé du type d'entité, de son identifiant, de sa version et de la version d'embedding. Une ancienne tâche ne peut ainsi pas écraser le vecteur d'une nouvelle version. Avant toute restitution, le service vérifie la version courante, l'existence et la visibilité dans MongoDB ; les points obsolètes sont ignorés, nettoyés et, si nécessaire, la recherche récupère davantage de candidats dans une limite explicite.

Une suppression ou une modification de visibilité déclenche également un événement. Le contrôle MongoDB bloque immédiatement l'exposition par l'application, même si Qdrant n'est pas encore à jour. Une réconciliation périodique supprime les points résiduels et répare les indexations manquantes. Les payloads Qdrant contiennent uniquement les données de recherche nécessaires, sans coordonnées privées. Qdrant reste accessible uniquement aux services autorisés.

En cas de panne Qdrant, les modifications métier et brouillons restent disponibles dans MongoDB et les événements sont conservés. La recherche signale son indisponibilité ou propose explicitement une recherche lexicale dégradée ; aucune correspondance sémantique n'est inventée. Toute reconstruction conserve les filtres de visibilité et les versions avant la bascule.

Prévoir des index sur les propriétaires et organisations, les références métier et versions, les filtres de recherche et le couple état/date de disponibilité des tâches. Ajouter des index uniques pour les clés d'idempotence et les versions d'un même dossier. Conserver les historiques et résultats volumineux dans des documents séparés, avec pagination, plutôt que dans des tableaux qui grandissent sans limite.

La consommation d'une approbation, la création d'une soumission et l'insertion dans l'outbox doivent être atomiques. Le plan retient des transactions MongoDB sur un replica set ou un cluster shardé compatible ; une instance standalone ne suffit pas pour ces transactions multi-documents. Le service propriétaire de l'action exécute cette transaction ; si les écritures passent par une API métier existante, son contrat doit fournir les mêmes garanties sans supposer une transaction répartie entre services.

Le worker réserve une tâche par `find_one_and_update` avec un état et une date d'éligibilité attendus. Il stocke un identifiant de réservation, une échéance de bail, un compteur de tentatives et une prochaine date d'exécution. Toute mise à jour de progression ou de résultat vérifie la réservation courante ; un worker dont le bail a expiré ne peut pas valider la tâche. La reprise après panne peut néanmoins rejouer un traitement : les effets persistants et les envois doivent donc rester idempotents. Les tâches en échec définitif sont conservées pour diagnostic.

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
6. Une clé d'idempotence et un index unique MongoDB empêchent de créer deux soumissions pour la même action. Une transaction consomme l'approbation et crée la soumission et son événement d'outbox ; l'appel externe intervient après le commit et utilise la même clé d'idempotence.
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

Appliquer les droits et les filtres explicites dans les branches lexicale et vectorielle, combiner leurs classements dans `SearchService`, puis charger les documents métier depuis MongoDB. Recontrôler leurs versions, leur état et leurs droits avant restitution, car les index peuvent avoir un retard de mise à jour. Une contrainte explicite n'est jamais assouplie silencieusement ; si aucun résultat ne correspond, proposer à l'utilisateur les filtres à élargir. Les messages de suivi peuvent modifier les critères d'une recherche existante.

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
| 0 — Cadrage et jeux d'essai | Schémas MongoDB, contrats d'intégration, topologie, évaluation Qdrant, objectifs de disponibilité et corpus | Propriété des écritures définie ; transactions et synchronisation spécifiées ; budget et scénarios de charge définis |
| 1 — Socle | Authentification ou intégration, PyMongo Async, collections et index IA, stockage existant, worker MongoDB, versions et validations | Impossible de consulter les CV d'un autre utilisateur ou d'envoyer sans confirmation ; reprise d'une tâche après perte du worker |
| 2 — CV et évaluation | Import, édition, acceptation, complétude et conseils | CV → profil validé ; champs absents préservés ; scan détecté ; projets valorisés |
| 3 — Dossier et soumission | Matrice de preuves, lettre, conseils CV, approbation et envoi | Une modification invalide l'approbation ; une relance ne crée pas deux candidatures |
| 4 — Recherche d'offres | Index Qdrant, synchronisation, interprétation des requêtes, filtres et recherche hybride | Requête PFE/Sfax correcte ; contraintes respectées ; zéro résultat et retard d'indexation traités clairement |
| 5 — Sourcing et top 5 | Recherche candidats, critères, score détaillé et explications | Alias reconnus ; compétences proches non confondues ; chaque justification sourcée |
| 6 — Pilote | Mesures de qualité, coût, charge, reprise après panne et corrections | Validation humaine des résultats ; objectifs de latence, restauration et retard d'indexation vérifiés avant ouverture |
| Option — Anonymisation | Projection et tests de fuite d'identité | Identité masquée dans les réponses, textes libres et documents accessibles |

La première démonstration complète doit suivre un seul parcours : importer un CV, valider le profil, choisir une offre, générer et corriger une lettre, puis confirmer une candidature. Le sourcing réutilise ensuite les mêmes profils validés et la même matrice de critères.

Une estimation calendaire fiable nécessite de connaître l'équipe, l'existence du frontend et des services métier, le volume de documents et la qualité des données. Chiffrer chaque lot après le cadrage ; éviter d'inclure implicitement la construction d'une plateforme entière dans le seul lot IA.

## 9. Vérification et exploitation

Constituer un corpus pilote autorisé ou synthétique : CV français/anglais, débutants, profils expérimentés, documents incomplets, scans, offres ambiguës et couples offre-profil annotés par un recruteur.

Mesurer séparément : exactitude de l'extraction par champ, respect des filtres, pertinence du top 5, fidélité des explications, affirmations sans preuve dans les lettres, coût par tâche et latence au 95e percentile. Fixer les seuils avec le corpus et le budget avant l'acceptation du pilote.

Les contrôles bloquants couvrent : publication sans approbation, validation d'une ancienne version, accès entre organisations, CV contenant des instructions malveillantes, double envoi, panne du fournisseur et perte du worker. Les contenus importés sont des données non fiables ; ils ne peuvent ni changer les consignes de l'agent ni déclencher un outil externe.

Les tests d'intégration MongoDB utilisent la topologie retenue, notamment un replica set pour les transactions. Vérifier également la réservation concurrente des tâches, l'expiration des baux, l'unicité des soumissions, le rollback approbation/outbox, la réindexation et la suppression effective d'un profil devenu invisible même si son index de recherche est encore en retard.

Les tests de synchronisation couvrent les événements dupliqués ou désordonnés, un arrêt après écriture Qdrant mais avant acquittement MongoDB, une suppression pendant le calcul d'embedding, la panne Qdrant, la réconciliation et la restauration d'un index complet. Les tests de charge vérifient les limites de concurrence et la qualité de recherche en même temps que la latence ; un résultat rapide mais incomplet ne constitue pas un succès.

Versionner modèles, prompts et barèmes. Limiter la concurrence et le nombre de tentatives ; conserver les brouillons si le fournisseur échoue. Prévoir la suppression du document, des données dérivées et des embeddings selon la politique de conservation définie. Les quotas et délais doivent produire des messages exploitables par l'utilisateur.

## 10. Décisions ouvertes

1. Schémas et modules métier déjà disponibles dans MongoDB ; service propriétaire des écritures et contrats d'intégration.
2. Groq primaire et Gemini de repli décidés pour la génération ; vérifier accès réel aux modèles, quotas, hébergement, traitement des CV et budget. Sélectionner séparément le modèle d'embedding.
3. Langues initiales et importance des CV scannés ; l'arabe doit être explicitement ajouté au corpus s'il entre dans le périmètre.
4. Canaux de soumission et de contact ; garanties d'idempotence de la plateforme destinataire.
5. Règles de visibilité des candidats et moment d'activation de l'affichage anonymisé.
6. Demande inconnue : définir une charge pilote de référence, des scénarios de croissance, des objectifs de latence, de fraîcheur d'indexation et de disponibilité.
7. MongoDB autohébergé confirmé : version et topologie pour les transactions ; mode d'exploitation Qdrant, dimensionnement initial et budget à préciser. L'architecture séparée est la recommandation actuelle, pas une infrastructure déjà déployée.

## Références techniques

- [Qdrant — déploiement distribué](https://qdrant.tech/documentation/scaling/distributed_deployment/) : distribution, réplication, placement des shards et limites d'automatisation en autohébergement.
- [MongoDB Vector Search — documentation officielle](https://www.mongodb.com/docs/vector-search/) : stockage des embeddings, recherche sémantique, filtres et combinaison avec la recherche textuelle ; disponibilité à vérifier sur le déploiement retenu.
- [PyMongo Async — documentation officielle](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/) : API asynchrone du pilote Python.
- [MongoDB — transactions et topologie](https://www.mongodb.com/docs/manual/core/transactions-production-consideration/) : prérequis des transactions multi-documents.
- [MongoDB Search autohébergé — compatibilité](https://www.mongodb.com/docs/search/self-managed/current/deployment/compatibility-requirements/) : versions, éditions et plateformes supportées pour `mongot`.
- [NIST — caractéristiques d'une IA digne de confiance](https://airc.nist.gov/airmf-resources/airmf/3-sec-characteristics/) : distinction entre explicabilité, validité et gestion des biais. L'anonymisation n'est pas une preuve d'impartialité.
