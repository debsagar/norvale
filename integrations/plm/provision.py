"""Create scoped local PLM identities via the upstream Django application shell."""
import os

from django.contrib.auth.models import Group, Permission
from users.models import RuleSet, User, UserProfile

for role in ("publisher", "reader", "engineering", "approver"):
    password = os.environ.get(f"PLM_{role.upper()}_PASSWORD")
    if password is None:
        continue
    writable = role != "reader"
    creates = role == "publisher"
    name = f"norvale-{role}"
    group, _ = Group.objects.get_or_create(name=name)
    RuleSet.objects.update_or_create(
        name="part", group=group,
        defaults={"can_view": True, "can_add": creates, "can_change": writable,
                  "can_delete": False},
    )
    # The upstream shell skips the permission-sync background hook.
    actions = ("view", "change") if writable else ("view",)
    if creates:
        actions += ("add",)
    group.permissions.set(Permission.objects.filter(
        content_type__app_label="part",
        codename__in=[f"{action}_{model}" for action in actions for model in ("part", "bomitem")],
    ))
    user, _ = User.objects.get_or_create(username=name)
    user.set_password(password)
    user.save()
    UserProfile.objects.get_or_create(user=user)
    user.groups.set([group])
