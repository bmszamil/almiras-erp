"""Thin input/output layer. Amounts are Decimal strings (R22); bilingual text is {"en", "ar"}."""
from rest_framework import serializers

from access.models import User
from core.models import Entity

from .models import ApprovalAction, ApprovableType, ApprovalPolicy, ApprovalRequest, ApprovalStep, Delegation, Notification


def bi(en, ar):
    return {"en": en, "ar": ar}


class UserRefSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "name"]

    def get_name(self, obj):
        return bi(obj.full_name_en, obj.full_name_ar)


class EntityRefSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = Entity
        fields = ["id", "code", "name"]

    def get_name(self, obj):
        return bi(obj.name_en, obj.name_ar)


class ActionSerializer(serializers.ModelSerializer):
    actor = UserRefSerializer()
    on_behalf_of = UserRefSerializer(allow_null=True)

    class Meta:
        model = ApprovalAction
        fields = ["id", "action", "step", "actor", "on_behalf_of", "comment", "self_approval_override", "at", "channel"]


class RequestListSerializer(serializers.ModelSerializer):
    entity = EntityRefSerializer()
    document_type = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True, allow_null=True)
    submitted_by = UserRefSerializer()
    step = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalRequest
        fields = ["id", "entity", "document_type", "object_id", "summary", "amount", "currency", "status",
                  "step", "submitted_by", "submitted_at", "decided_at", "source"]

    def get_document_type(self, obj):
        return {"code": obj.document_type.code, "name": bi(obj.document_type.name_en, obj.document_type.name_ar)}

    def get_summary(self, obj):
        return bi(obj.summary_en, obj.summary_ar)

    def get_step(self, obj):
        seqs = sorted(set(obj.policy.steps.values_list("sequence", flat=True)))
        current = seqs.index(obj.current_sequence) + 1 if obj.current_sequence in seqs else None
        return {"current": current, "total": len(seqs)}


class RequestDetailSerializer(RequestListSerializer):
    document_version = serializers.CharField()
    actions = ActionSerializer(many=True)
    steps = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta(RequestListSerializer.Meta):
        fields = RequestListSerializer.Meta.fields + ["document_version", "actions", "steps", "allowed_actions"]

    def get_steps(self, obj):
        return [
            {
                "id": s.pk, "sequence": s.sequence, "min_approvals": s.min_approvals,
                "approver_role": s.approver_role.code if s.approver_role_id else None,
                "approver_user": s.approver_user.username if s.approver_user_id else None,
                "escalate_after_hours": s.escalate_after_hours,
            }
            for s in obj.policy.steps.all()
        ]

    def get_allowed_actions(self, obj):
        from .services import allowed_actions

        return allowed_actions(obj, self.context["request"].user)


class SubmitSerializer(serializers.Serializer):
    document_type = serializers.CharField()
    object_id = serializers.CharField()


class DecisionSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    document_version = serializers.CharField()
    on_behalf_of = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False, allow_null=True)


class CancelSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class DelegationSerializer(serializers.ModelSerializer):
    document_types = serializers.SlugRelatedField(slug_field="code", many=True, required=False,
                                                  queryset=ApprovableType.objects.all())
    amount_max = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True,
                                          allow_null=True, required=False)
    delegator = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False)

    class Meta:
        model = Delegation
        fields = ["id", "delegator", "delegate", "entity", "document_types", "amount_max", "currency",
                  "starts_at", "ends_at", "reason", "created_by", "created_at"]
        read_only_fields = ["created_by", "created_at"]


class StepSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovalStep
        fields = ["id", "sequence", "approver_role", "approver_user", "min_approvals",
                  "escalate_after_hours", "escalate_to_role", "escalate_to_user"]


class PolicySerializer(serializers.ModelSerializer):
    steps = StepSerializer(many=True)
    document_type = serializers.SlugRelatedField(slug_field="code", queryset=ApprovableType.objects.all())
    amount_min = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True, allow_null=True, required=False)
    amount_max = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True, allow_null=True, required=False)

    class Meta:
        model = ApprovalPolicy
        fields = ["id", "entity", "document_type", "amount_min", "amount_max", "currency", "priority",
                  "name_en", "name_ar", "valid_from", "valid_to", "expire_after_hours", "is_active", "steps"]


class NotificationSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    body = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "entity", "kind", "alert_code", "title", "body", "link", "created_at", "read_at"]

    def get_title(self, obj):
        return bi(obj.title_en, obj.title_ar)

    def get_body(self, obj):
        return bi(obj.body_en, obj.body_ar)
