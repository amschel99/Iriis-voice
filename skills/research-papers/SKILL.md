---
name: research-papers
description: Find, read and explain AI / computer-science research papers (arXiv, Hugging Face paper pages), what's trending in research, and the code, models and demos behind a paper.
keywords: [paper, papers, arxiv, research paper, study, preprint, state of the art, sota, latest research, new research, daily papers, trending papers, benchmark, technique, method, neurips, iclr, icml, acl, cvpr, huggingface, hugging face]
servers: [research]
source: adapted from huggingface/skills huggingface-papers (Apache-2.0)
---
# Research papers

**Find.** `research__search_papers(query)` searches titles, authors and content. For "what's new", use
`research__daily_papers()` (trending by upvotes). An arXiv ID (2602.08025), an arxiv.org link or a
huggingface.co/papers link all work as the paper argument.

**Read.** `research__read_paper(paper)` gives the full text in parts; use `find="method"`, `find="results"`,
`find="limitations"` to jump. Read the abstract, the method, the main results table and the limitations before
you explain a paper. Don't explain a paper you have only seen the title of.

**Use.** `research__paper_details(paper)` lists the code repo, project page and the models, datasets and Spaces built
on it: that's how to tell whether a technique is usable today.

**Explain it well.**
- One line: what problem, what they did, how much better (with the number and the benchmark).
- Then how it works in plain words, then what it needs (data, compute, a GPU?), then the catch (limitations, what
  wasn't tested).
- Say how established it is: date, upvotes, whether code exists, whether others have built on it. A two-week-old
  preprint is a lead, not settled fact.
- For "how could this improve you / my project": map the idea to the concrete part it would change, and say what it
  would cost to try.

When you learn a technique worth keeping for your own work, remember it (`memory__remember` or your playbook) with
the paper ID.
