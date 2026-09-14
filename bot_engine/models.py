from contextlib import contextmanager
from datetime import timedelta
import hashlib
import io
import logging
import os

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class ResearchPoster(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    ANALYSIS_STATUS_CHOICES = [
        ("ok", "OK"),
        ("failed", "Failed"),
        ("processing", "Processing"),
    ]

    UPLOAD_SOURCE_CHOICES = [
        ("web", "Web"),
        ("telegram", "Telegram"),
        ("whatsapp", "WhatsApp"),
    ]

    CATEGORY_CHOICES = [
        ("ai_ml", "AI & Machine Learning"),
        ("cv_graphics", "Computer Vision & Graphics"),
        ("systems_arch", "Systems & Architecture"),
        ("networks_security", "Networks & Security"),
        ("data_info", "Data & Information"),
        ("software_algo", "Software & Algorithms"),
        ("computing_paradigms", "Computing Paradigms"),
        ("interdisciplinary", "Interdisciplinary & Applied"),
        ("other", "Other"),
    ]

    SUBFIELDS_GROUPED = [
        ("ai_ml", "AI & Machine Learning", [
            ("artificial_intelligence", "Artificial Intelligence"),
            ("machine_learning", "Machine Learning"),
            ("deep_learning", "Deep Learning"),
            ("reinforcement_learning", "Reinforcement Learning"),
            ("nlp", "Natural Language Processing"),
            ("expert_systems", "Expert Systems"),
            ("knowledge_representation", "Knowledge Representation"),
            ("generative_model", "Generative Model"),
            ("continual_learning", "Continual Learning"),
            ("foundation_model", "Foundation Model"),
            ("model_merging", "Model Merging"),
            ("mil", "MIL"),
        ]),
        ("cv_graphics", "Computer Vision & Graphics", [
            ("computer_vision", "Computer Vision"),
            ("medical_imaging", "Medical Imaging"),
            ("image_processing", "Image Processing"),
            ("computer_graphics", "Computer Graphics"),
            ("augmented_reality", "Augmented Reality"),
            ("virtual_reality", "Virtual Reality"),
            ("segmentation", "Segmentation"),
            ("classification", "Classification"),
            ("vision_text", "Vision-Text"),
            ("mri", "MRI"),
            ("cbct", "CBCT"),
            ("pet", "PET"),
            ("xray", "X-ray"),
            ("wsi", "WSI"),
            ("ct", "CT"),
            ("us", "US"),
        ]),
        ("systems_arch", "Systems & Architecture", [
            ("distributed_systems", "Distributed Systems"),
            ("embedded_systems", "Embedded Systems"),
            ("computer_architecture", "Computer Architecture"),
            ("operating_systems", "Operating Systems"),
            ("parallel_computing", "Parallel Computing"),
            ("hpc", "High-Performance Computing"),
            ("dependable_systems", "Dependable Systems"),
        ]),
        ("networks_security", "Networks & Security", [
            ("cybersecurity", "Cybersecurity"),
            ("cryptography", "Cryptography"),
            ("blockchain", "Blockchain Technology"),
            ("network_security", "Network Security"),
            ("iot", "Internet of Things"),
            ("edge_computing", "Edge Computing"),
        ]),
        ("data_info", "Data & Information", [
            ("data_mining", "Data Mining"),
            ("big_data", "Big Data Analytics"),
            ("dbms", "Database Management Systems"),
            ("information_retrieval", "Information Retrieval"),
            ("multimodal", "Multimodal"),
            ("missing_modalities", "Missing Modalities"),
            ("report", "Report"),
            ("challenge", "Challenge"),
        ]),
        ("software_algo", "Software & Algorithms", [
            ("software_engineering", "Software Engineering"),
            ("algorithm_design", "Algorithm Design"),
            ("computational_complexity", "Computational Complexity"),
            ("formal_methods", "Formal Methods"),
            ("software_testing", "Software Testing"),
        ]),
        ("computing_paradigms", "Computing Paradigms", [
            ("cloud_computing", "Cloud Computing"),
            ("quantum_computing", "Quantum Computing"),
            ("neuromorphic_computing", "Neuromorphic Computing"),
            ("mobile_computing", "Mobile Computing"),
            ("wearable_computing", "Wearable Computing"),
            ("pervasive_computing", "Pervasive Computing"),
        ]),
        ("interdisciplinary", "Interdisciplinary & Applied", [
            ("robotics", "Robotics"),
            ("autonomous_systems", "Autonomous Systems"),
            ("bioinformatics", "Bioinformatics"),
            ("computational_biology", "Computational Biology"),
            ("hci", "Human-Computer Interaction"),
            ("speech_recognition", "Speech Recognition"),
            ("signal_processing", "Signal Processing"),
            ("smart_grids", "Smart Grids"),
            ("cyber_physical_systems", "Cyber-Physical Systems"),
            ("brain", "Brain"),
            ("abdomen", "Abdomen"),
            ("maxillofacial", "Maxillofacial"),
        ]),
    ]

    SUBFIELD_CHOICES = [
        pair for _, _, items in SUBFIELDS_GROUPED for pair in items
    ]

    SUBFIELD_TO_CATEGORY = {
        sf_slug: cat_slug
        for cat_slug, _, items in SUBFIELDS_GROUPED
        for sf_slug, _ in items
    }

    image = models.ImageField(upload_to="posters/")
    thumbnail = models.ImageField(upload_to="posters/thumbs/", blank=True, null=True)
    image_sha256 = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    title = models.CharField(max_length=255)
    authors = models.TextField()
    paper_link = models.URLField(max_length=500, blank=True, null=True)
    github_link = models.URLField(max_length=500, blank=True, null=True)
    ai_paper_link = models.URLField(max_length=500, blank=True, null=True)
    ai_github_link = models.URLField(max_length=500, blank=True, null=True)
    summary = models.TextField()
    why_useful = models.TextField(blank=True, null=True)

    validation_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        db_index=True,
    )
    analysis_status = models.CharField(
        max_length=20,
        choices=ANALYSIS_STATUS_CHOICES,
        default="ok",
        db_index=True,
    )

    upload_source = models.CharField(
        max_length=20,
        choices=UPLOAD_SOURCE_CHOICES,
        default="web",
        db_index=True,
    )
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default="other", db_index=True)
    subfields = models.CharField(max_length=500, blank=True, default="")
    tags = models.CharField(max_length=200, blank=True, null=True)
    publication_year = models.PositiveSmallIntegerField(blank=True, null=True, db_index=True)
    conference = models.CharField(max_length=200, blank=True, default="", db_index=True)
    notes = models.TextField(max_length=500, blank=True, null=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_posters",
    )

    processing_token = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    processing_started_at = models.DateTimeField(blank=True, null=True, db_index=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    analysis_error = models.TextField(blank=True, null=True)

    last_success_notified_at = models.DateTimeField(blank=True, null=True)
    last_failure_notified_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["analysis_status", "created_at"]),
            models.Index(fields=["upload_source", "created_at"]),
            models.Index(fields=["validation_status", "created_at"]),
        ]

    @property
    def is_new(self):
        return timezone.now() - self.created_at < timedelta(hours=24)

    @property
    def subfields_list(self):
        if not self.subfields:
            return []

        seen = set()
        result = []

        for s in self.subfields.split(","):
            s = s.strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)

        return result

    @property
    def subfields_display(self):
        slug_to_label = dict(self.SUBFIELD_CHOICES)
        return [slug_to_label.get(s, s) for s in self.subfields_list]

    def normalize_subfields(self):
        self.subfields = ",".join(self.subfields_list)

    @property
    def tags_list(self):
        if not self.tags:
            return []
        excluded = {s.lower() for s in self.subfields_display} | {s.lower() for s in self.subfields_list}
        seen, result = set(), []
        for t in self.tags.split(","):
            t = t.strip()
            key = t.lower()
            if t and key not in excluded and key not in seen:
                seen.add(key)
                result.append(t)
        return result

    def derive_category_from_subfields(self):
        self.category = next(
            (cat for sf in self.subfields_list if (cat := self.SUBFIELD_TO_CATEGORY.get(sf))),
            "other",
        )

    @contextmanager
    def _open_image(self):
        self.image.open("rb")
        try:
            yield self.image
        finally:
            try:
                self.image.close()
            except (OSError, ValueError) as e:
                logger.warning("Could not close image for poster %s: %s", self.pk, type(e).__name__)

    def generate_thumbnail(self, max_size=300, quality=80, save=False):
        if not self.image:
            return
        from PIL import Image, UnidentifiedImageError
        try:
            with self._open_image() as f, Image.open(f) as img:
                img.thumbnail((max_size, max_size), Image.LANCZOS)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=quality)
                thumb_name = f"thumb_{os.path.splitext(os.path.basename(self.image.name))[0]}.jpg"
                self.thumbnail.save(thumb_name, ContentFile(buf.getvalue()), save=save)
        except (UnidentifiedImageError, SuspiciousFileOperation, OSError, ValueError) as e:
            logger.warning("Thumbnail generation failed for poster %s: %s", self.pk, type(e).__name__)

    @property
    def thumbnail_url(self):
        if self.thumbnail:
            return self.thumbnail.url
        return self.image.url if self.image else ""

    def compute_image_sha256(self):
        if not self.image:
            return None
        hasher = hashlib.sha256()
        with self._open_image() as f:
            for chunk in f.chunks():
                hasher.update(chunk)
        return hasher.hexdigest()

    def ensure_image_sha256(self, save=False):
        if self.image and not self.image_sha256:
            self.image_sha256 = self.compute_image_sha256()
            if save:
                self.save(update_fields=["image_sha256", "updated_at"])
        return self.image_sha256

    @classmethod
    def find_existing_by_hash(cls, image_sha256, exclude_id=None, group_ids=None):
        if not image_sha256:
            return None
        qs = cls.objects.filter(image_sha256=image_sha256)
        if exclude_id:
            qs = qs.exclude(pk=exclude_id)
        if group_ids is not None:
            qs = qs.filter(groups__id__in=group_ids)
        return qs.order_by("created_at").first()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "subfields" in update_fields or "category" in update_fields:
            self.normalize_subfields()
            self.derive_category_from_subfields()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class Favorite(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorite_posters",
    )
    poster = models.ForeignKey(
        ResearchPoster,
        on_delete=models.CASCADE,
        related_name="favorites",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "poster"],
                name="unique_user_poster_favorite",
            )
        ]

    def __str__(self):
        return f"{self.user} -> {self.poster}"


class ActivityLog(models.Model):
    ACTION_CHOICES = [
        ("created", "Created"),
        ("updated", "Updated"),
        ("deleted", "Deleted"),
        ("status_changed", "Status Changed"),
        ("favorited", "Favorited"),
        ("unfavorited", "Unfavorited"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poster_activity_logs",
    )
    poster = models.ForeignKey(
        ResearchPoster,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="activity_logs",
    )
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    details = models.TextField(blank=True, null=True)
    poster_title = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["action", "timestamp"]),
            models.Index(fields=["poster", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.action} - {self.poster_title or 'Unknown'} - {self.timestamp}"


class ResearchGroup(models.Model):
    name = models.CharField(max_length=150, unique=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="UserGroupMembership",
        related_name="research_groups",
    )
    posters = models.ManyToManyField(
        ResearchPoster,
        related_name="groups",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Research group"
        verbose_name_plural = "Research groups"

    def __str__(self):
        return self.name

    @property
    def research_interests_text(self):
        return "\n".join(
            i.text for i in self.interests.all() if i.text and i.text.strip()
        )


class ResearchInterest(models.Model):
    group = models.ForeignKey(
        ResearchGroup,
        on_delete=models.CASCADE,
        related_name="interests",
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return (self.text or "")[:60]


class UserGroupMembership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    group = models.ForeignKey(
        ResearchGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    is_primary = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "group"],
                name="unique_user_group_membership",
            ),
        ]
        ordering = ["-is_primary", "joined_at"]

    def __str__(self):
        primary = " (primary)" if self.is_primary else ""
        return f"{self.user} -> {self.group}{primary}"

    def save(self, *args, **kwargs):
        if not self.is_primary:
            super().save(*args, **kwargs)
            return
        with transaction.atomic():
            UserGroupMembership.objects.filter(
                user=self.user, is_primary=True,
            ).exclude(pk=self.pk).update(is_primary=False)
            super().save(*args, **kwargs)


class PosterGroupWhyUseful(models.Model):
    poster = models.ForeignKey(
        ResearchPoster,
        on_delete=models.CASCADE,
        related_name="group_why_useful_entries",
    )
    group = models.ForeignKey(
        ResearchGroup,
        on_delete=models.CASCADE,
        related_name="poster_why_useful_entries",
    )
    why_useful = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["poster", "group"],
                name="unique_poster_group_why_useful",
            ),
        ]

    def __str__(self):
        return f"WhyUseful: {self.poster} / {self.group}"


class PendingAssignmentDismissal(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_dismissal",
    )
    dismissed_at = models.DateTimeField(auto_now_add=True)
    dismissed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-dismissed_at"]

    def __str__(self):
        return f"Dismissed: {self.user}"


class BotAccount(models.Model):
    PLATFORM_CHOICES = [
        ("telegram", "Telegram"),
        ("whatsapp", "WhatsApp"),
    ]
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    recipient = models.CharField(max_length=64, help_text="chat_id for Telegram, phone for WhatsApp")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="bot_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "recipient"],
                name="unique_bot_account",
            ),
        ]
        indexes = [
            models.Index(fields=["platform", "recipient"]),
        ]

    def __str__(self):
        return f"{self.platform}:{self.recipient} -> {self.user}"
