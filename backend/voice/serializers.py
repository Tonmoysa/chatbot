from rest_framework import serializers


class VoiceTranscribeRequestSerializer(serializers.Serializer):
    file = serializers.FileField()
    language = serializers.CharField(max_length=16, required=False, allow_blank=True)
