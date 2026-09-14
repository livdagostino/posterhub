from django.db.models import Q
from django.shortcuts import get_object_or_404

from .models import ResearchPoster, UserGroupMembership

GROUP_MANAGER_ROLE = "GestoreGruppi"


def is_group_manager(user):
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser or user.groups.filter(name=GROUP_MANAGER_ROLE).exists()


def user_can_interact(user):
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser or UserGroupMembership.objects.filter(user=user).exists()


def user_group_ids(user):
    if not user or not user.is_authenticated:
        return []
    return list(
        UserGroupMembership.objects
        .filter(user=user)
        .values_list("group_id", flat=True)
    )


def accessible_posters(user, queryset=None):
    base = ResearchPoster.objects.all() if queryset is None else queryset
    if not user or not user.is_authenticated:
        return base.none()
    if user.is_superuser:
        return base
    return base.filter(
        Q(groups__id__in=user_group_ids(user)) | Q(uploaded_by=user)
    ).distinct()


def get_accessible_poster_or_404(user, poster_id, queryset=None):
    return get_object_or_404(accessible_posters(user, queryset), pk=poster_id)


def can_access_group(user, group_id):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return UserGroupMembership.objects.filter(user=user, group_id=group_id).exists()
