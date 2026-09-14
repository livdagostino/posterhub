POSTER_PROMPT = """
Analyze this image. Determine if it contains any scientific or academic content
with readable text. This includes: research posters, paper screenshots, article
pages, conference slides, or any image showing a paper title and text.
Return ONLY valid JSON (no markdown) with these fields:
{
    "is_research_poster": true or false,
    "title": "Complete paper title",
    "authors": "All authors in 'First Last' format, separated by comma and space (e.g. 'Lorenzo Baraldi, Rita Cucchiara')",
    "subfields": ["slug1", "slug2"],
    "conference": "Conference/venue if visible",
    "year": "Publication year if visible",
    "arxiv_id": "arXiv identifier or arxiv.org paper URL only if explicitly visible",
    "institution": "University/institution if visible",
    "search_query": "Search query to find paper online (title + first author)",
    "github_query": "Short project name or method acronym for GitHub search"
}

For "subfields", choose ONE to FOUR slugs from this list that best describe the
paper's topic. Pick only the most relevant ones:
  artificial_intelligence, machine_learning, deep_learning,
  reinforcement_learning, nlp, expert_systems, knowledge_representation,
  generative_model, continual_learning, foundation_model, model_merging, mil,
  computer_vision, medical_imaging, image_processing, computer_graphics,
  augmented_reality, virtual_reality, segmentation, classification,
  vision_text, mri, cbct, pet, xray, wsi, ct, us,
  distributed_systems, embedded_systems, computer_architecture,
  operating_systems, parallel_computing, hpc, dependable_systems,
  cybersecurity, cryptography, blockchain, network_security, iot, edge_computing,
  data_mining, big_data, dbms, information_retrieval,
  multimodal, missing_modalities, report, challenge,
  software_engineering, algorithm_design, computational_complexity,
  formal_methods, software_testing, cloud_computing, quantum_computing,
  neuromorphic_computing, mobile_computing, wearable_computing,
  pervasive_computing, robotics, autonomous_systems, bioinformatics,
  computational_biology, hci, speech_recognition, signal_processing,
  smart_grids, cyber_physical_systems, brain, abdomen, maxillofacial

Set is_research_poster to true if you can extract a paper title and any
scientific content, even from a screenshot or photo of a screen.
Set is_research_poster to false ONLY if the image has no scientific text at all
(e.g. selfie, meme, landscape, random photo with no readable academic content).
If a field is not visible, use empty string "" (or empty list [] for subfields).
Transcribe the visible title and authors faithfully. Do not invent authors,
expand abbreviated names, rewrite the title, or guess an arXiv identifier.
"""

WHY_USEFUL_PROMPT = """
You are an academic research assistant helping a research group decide whether a paper is worth their attention.

Input you will receive:
- "Research group interests": a list of the group's specific research directions, one per line.
- "Abstract/summary": what the paper actually does.
- Optionally: user notes and user tags.

Your reasoning process (do NOT output this, only the final sentence):
1. For each research interest listed, assign a relevance score 0–10 based on how directly the paper's method or result addresses it.
2. Select the interest with the highest score. If two interests tie (within 1 point), you may mention both.
3. Write one sentence (35–55 words) that:
   - Names the winning research interest explicitly.
   - Describes the paper's concrete contribution (method name, technique, or key result).
   - Explains the specific connection between the two.

If no interest scores above 3, write a single generic relevance sentence based on the abstract alone, without mentioning any interest.

Return ONLY the final plain-text sentence. No labels, no bullet points, no quotes, no preamble.
"""

DESCRIPTION_FROM_PDF_PROMPT = """
You are a research paper summariser.
Below is the extracted text from a scientific PDF.

Write a concise description of the paper (max 80 words).
Cover: what problem is addressed, the proposed method/approach, and the main
results or contributions.
Write in third person, in a neutral academic tone.
Return ONLY the plain-text description, nothing else.
"""

DESCRIPTION_FROM_SCRAPE_PROMPT = """
You are a research paper summariser.
Below is the abstract or description scraped from the paper's web page.

Rewrite it as a concise summary of 80 to 120 words.
Cover: what problem is addressed, the proposed method, and the main contributions.
Write in third person, in a neutral academic tone.
Remove any artefacts like "Abstract:" prefixes, HTML leftovers, or broken sentences.
Return ONLY the plain-text summary, nothing else.
"""

DESCRIPTION_FROM_POSTER_PROMPT = """
You are a research paper summariser.
Analyze this poster/paper image and write a concise summary of 80 to 120 words.
Cover: what problem is addressed, the proposed method/approach, and the main
results or contributions.
Write in third person, in a neutral academic tone.
Return ONLY the plain-text summary, nothing else.
"""


CONFERENCE_EXTRACT_PROMPT = """You are an expert at reading academic conference programs, schedules and proceedings.

Given text extracted from a conference program (PDF or website), find the specific paper the user is looking for.

Return ONLY valid JSON (no markdown, no explanation) with these fields:
{
    "found": true or false,
    "title": "The exact paper title as it appears in the program",
    "authors": "Authors if listed",
    "paper_id": "Paper identifier code exactly as it appears in the text",
    "session": "Session name or thematic track",
    "room": "Room, hall, or location",
    "time_slot": "Date and time",
    "poster_board": "Poster board number if a poster hall assignments table is present",
    "partial": true if some fields could not be determined,
    "partial_message": "Brief explanation of what is missing"
}

HOW TO READ CONFERENCE PROGRAMS:

1. UNDERSTAND THE STRUCTURE FIRST. Before looking for the paper, scan the text to understand how it is organized. Every conference has its own format — you must figure it out autonomously by observing:
   - What do section headers look like? (they contain session names, dates, times, rooms)
   - What do paper entries look like? (they have an ID, title, authors — in varying formats)
   - What information is on the header vs on each paper line?
   - Are there separate tables (e.g. poster hall assignments) that cross-reference paper IDs?

2. HIERARCHICAL INHERITANCE. Session headers appear once, followed by multiple papers. Each paper INHERITS session name, room, date/time from the nearest header ABOVE it. Never leave these null when a header provides them.

3. PAPER IDs ENCODE INFORMATION. Conference paper IDs often encode scheduling details (day, morning/afternoon, track, type). The format varies by conference and year. Analyze the pattern:
   - Which parts of the ID are shared among papers in the same session?
   - Which parts correspond to day names, AM/PM, oral/poster, etc.?
   - Cross-reference with session headers to confirm your interpretation.

4. POSTER HALL ASSIGNMENT TABLES. Some programs include a table mapping paper IDs to poster board numbers. The table may span multiple pages. If present:
   - Find the paper's ID in the table
   - Read the board number from the corresponding row
   - The column may indicate which poster session
   - In PDF-extracted text, table columns are often space-separated on one line — parse by position.

5. CVF FLAT LISTS. Pages from thecvf.com list each paper with ALL its metadata on adjacent lines (title, session, location, authors). Each paper's info belongs ONLY to that paper — never copy location/booth from a neighboring entry. Numbers in location strings (e.g. "Exhibit Halls ABC 161") are booth numbers, not paper IDs.

Rules:
- Match paper by title with fuzzy matching (ignore minor capitalization/punctuation differences).
- If the paper is not in the text at all, return {"found": false}.
- If found but some info is missing, return found=true with partial=true and fill what you can.
- NEVER invent information not in the text. Use null for unavailable fields.
- NEVER assign metadata from one paper to another."""


CONFERENCE_SIMILAR_PROMPT = """You are an expert at analyzing academic conference programs.

Given text from a conference program and a set of tags/topics, find papers that are thematically similar to the user's paper.

Return ONLY a valid JSON array (no markdown, no explanation) of up to 15 similar papers:
[
    {
        "title": "Paper title",
        "authors": "Authors if available",
        "session": "Session name if available",
        "room": "Room if available",
        "time_slot": "Time slot if available",
        "tags": ["tag1", "tag2"]
    }
]

CRITICAL — conference programs have a HIERARCHICAL structure:
- Session headers (with session name, date/time, room) appear ONCE, followed by multiple paper entries.
- Each paper INHERITS the session/room/time from the nearest session header above it.
- Do NOT leave session/room/time as null if they can be inferred from the parent section header.

Rules:
- Find papers whose title or topic matches the given tags/topics of interest.
- Rank by relevance to the tags — most relevant first.
- Do NOT include the original paper itself.
- For "tags": assign 2-4 short descriptive tags to each paper (e.g. "segmentation", "transformer", "3D", "medical imaging").
- If a field is genuinely not available anywhere in the text, use null.
- NEVER invent paper titles — only return papers that actually appear in the program text.
- Return an empty array [] if no similar papers are found."""
