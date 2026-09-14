from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bot_engine.models import ResearchPoster
from bot_engine.paper_search import find_paper_from_github, search_paper
from bot_engine.utils_ai import _resolve_year


class Command(BaseCommand):
    help = "Find missing paper links for explicit IDs. Preview by default; --apply saves verified matches."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="+", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        ids = list(dict.fromkeys(options["ids"]))
        posters = {p.pk: p for p in ResearchPoster.objects.filter(pk__in=ids)}
        missing = set(ids) - posters.keys()
        if missing:
            raise CommandError(f"Unknown poster IDs: {sorted(missing)}")
        for pk in ids:
            poster = posters[pk]
            if poster.paper_link:
                self.stdout.write(f"{pk}: already has a paper link; unchanged")
                continue
            if poster.analysis_status == "processing":
                self.stdout.write(f"{pk}: analysis is running; skipped")
                continue
            paper = search_paper(poster.title)
            if not paper and poster.github_link:
                paper = find_paper_from_github(poster.github_link, poster.title)
            if not paper:
                self.stdout.write(self.style.WARNING(f"{pk}: no verified match; unchanged"))
                continue
            url = paper["paper_url"]
            self.stdout.write(f"{pk}: {paper['title']} -> {url}")
            if not options["apply"]:
                continue
            with transaction.atomic():
                current = ResearchPoster.objects.select_for_update().get(pk=pk)
                if (current.paper_link or current.title != poster.title
                        or current.analysis_status == "processing"):
                    self.stdout.write(f"{pk}: changed while searching; skipped")
                    continue
                current.paper_link = url
                current.ai_paper_link = url
                current.updated_at = timezone.now()
                fields = ["paper_link", "ai_paper_link", "updated_at"]
                if not current.publication_year:
                    year = _resolve_year("", paper, url)
                    if year:
                        current.publication_year = year
                        fields.append("publication_year")
                current.save(update_fields=fields)
                self.stdout.write(self.style.SUCCESS(f"{pk}: saved"))
