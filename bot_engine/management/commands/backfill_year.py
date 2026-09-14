import re
from django.core.management.base import BaseCommand
from bot_engine.models import ResearchPoster


class Command(BaseCommand):
    help = "Backfill publication_year from notes and paper_link for existing posters"

    def handle(self, *args, **options):
        posters = ResearchPoster.objects.filter(publication_year__isnull=True)

        updated = 0
        for p in posters:
            year = None

            if p.notes:
                match = re.search(r'Year:\s*(\d{4})', p.notes)
                if match:
                    year = int(match.group(1))

            if not year and p.paper_link:
                arxiv_match = re.search(
                    r'arxiv\.org/(?:abs|pdf)/(\d{2})\d{2}\.\d+', p.paper_link, re.I
                )
                if arxiv_match:
                    year = 2000 + int(arxiv_match.group(1))

            if year:
                p.publication_year = year
                p.save(update_fields=["publication_year", "updated_at"])
                updated += 1
                self.stdout.write(f"  #{p.id} '{p.title[:50]}' -> {year}")

        self.stdout.write(self.style.SUCCESS(f"Done. Updated {updated} posters."))
