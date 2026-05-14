from django.contrib import admin

from knowledge_base.models import KnowledgeChunk, KnowledgeDocument


class KnowledgeChunkInline(admin.TabularInline):
    model = KnowledgeChunk
    extra = 0
    readonly_fields = ("chunk_index", "token_count", "qdrant_point_id", "language", "created_at")
    fields = ("chunk_index", "token_count", "qdrant_point_id", "language", "created_at")


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "document_type", "status", "total_chunks", "uploaded_at")
    list_filter = ("status", "document_type")
    search_fields = ("title", "source_path", "checksum")
    readonly_fields = ("uploaded_at", "checksum", "total_chunks")
    inlines = [KnowledgeChunkInline]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ("document", "chunk_index", "token_count", "language", "created_at")
    list_filter = ("language",)
    search_fields = ("chunk_text", "qdrant_point_id")
