import base64
import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from ..config import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, VALID_MATCH_MODES
from .db import Jsonb, clean_tag_list, db_connect, ensure_db_ready, normalize_tag, parse_csv_tags

try:
    from psycopg.rows import dict_row
except ImportError:
    dict_row = None


IMAGE_SORTS = {"path_asc", "path_desc", "date_desc", "date_asc", "size_desc", "size_asc"}


def normalize_color(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    color = str(value).strip()
    if not color:
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(400, "Color must be a hex value like #E5E5E5")
    return color.upper()


def ensure_tag(cur, name: str, *, user_defined: bool = False) -> Optional[int]:
    name = " ".join(name.strip().split())
    norm = normalize_tag(name)
    if not name or not norm:
        return None
    if user_defined:
        cur.execute("DELETE FROM suppressed_auto_tags WHERE normalized = %s", (norm,))
    cur.execute(
        """
        INSERT INTO tags (name, normalized, user_defined)
        VALUES (%s, %s, %s)
        ON CONFLICT (normalized) DO UPDATE SET
            name = CASE WHEN EXCLUDED.user_defined THEN EXCLUDED.name ELSE tags.name END,
            user_defined = tags.user_defined OR EXCLUDED.user_defined
        RETURNING id
        """,
        (name, norm, user_defined),
    )
    row = cur.fetchone()
    return row[0] if row else None


def filter_suppressed_auto_tags(cur, tags: list[str]) -> list[str]:
    cleaned = clean_tag_list(tags)
    if not cleaned:
        return []
    normalized = [normalize_tag(tag) for tag in cleaned]
    cur.execute(
        "SELECT normalized FROM suppressed_auto_tags WHERE normalized = ANY(%s)",
        (normalized,),
    )
    suppressed = {row[0] for row in cur.fetchall()}
    return [tag for tag in cleaned if normalize_tag(tag) not in suppressed]


def cleanup_hidden_image_tag_data(cur) -> None:
    cur.execute(
        """
        DELETE FROM image_tags it
        USING images i
        WHERE it.image_id = i.id
          AND i.hidden = true
        """
    )
    cur.execute(
        """
        DELETE FROM tags t
        WHERE t.user_defined = false
          AND NOT EXISTS (
              SELECT 1
              FROM image_tags it
              WHERE it.tag_id = t.id
          )
        """
    )


def tag_summary_rows() -> list[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    t.color,
                    t.user_defined,
                    COUNT(DISTINCT CASE WHEN i.id IS NOT NULL THEN it.image_id END) AS image_count,
                    COUNT(DISTINCT CASE WHEN i.id IS NOT NULL AND it.kind = 'auto' THEN it.image_id END) AS auto_count,
                    COUNT(DISTINCT CASE WHEN i.id IS NOT NULL AND it.kind = 'user' THEN it.image_id END) AS user_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                LEFT JOIN images i ON i.id = it.image_id AND i.hidden = false
                GROUP BY t.id
                HAVING t.user_defined OR COUNT(DISTINCT i.id) > 0
                ORDER BY lower(t.name), t.name
                """
            )
            rows = cur.fetchall()
    return [
        {
            "name": row["name"],
            "normalized": row["normalized"],
            "color": row["color"],
            "image_count": int(row["image_count"] or 0),
            "auto_count": int(row["auto_count"] or 0),
            "user_count": int(row["user_count"] or 0),
            "user_defined": bool(row["user_defined"]),
            "is_auto": int(row["auto_count"] or 0) > 0,
        }
        for row in rows
    ]


def tag_summary_by_norm(norm: str) -> Optional[dict[str, Any]]:
    for row in tag_summary_rows():
        if row["normalized"] == norm:
            return row
    return None


def create_user_tag_entry(name: str) -> dict[str, Any]:
    ensure_db_ready()
    cleaned = clean_tag_list([name])
    if not cleaned:
        raise HTTPException(400, "Tag name is empty")
    with db_connect() as conn:
        with conn.cursor() as cur:
            ensure_tag(cur, cleaned[0], user_defined=True)
    return tag_summary_by_norm(normalize_tag(cleaned[0])) or {
        "name": cleaned[0],
        "color": None,
        "image_count": 0,
        "auto_count": 0,
        "user_count": 0,
        "user_defined": True,
        "is_auto": False,
    }


def update_tag_definition(
    tag: str,
    *,
    name: Optional[str],
    color: Optional[str],
    has_name: bool,
    has_color: bool,
) -> dict[str, Any]:
    ensure_db_ready()
    new_name: Optional[str] = None
    if has_name and name is not None:
        cleaned = clean_tag_list([name])
        if not cleaned:
            raise HTTPException(400, "Tag name is empty")
        new_name = cleaned[0]

    new_color = normalize_color(color) if has_color else None
    source_norm = normalize_tag(tag)
    final_norm = source_norm

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                WHERE t.normalized = %s
                GROUP BY t.id
                """,
                (source_norm,),
            )
            source = cur.fetchone()
            if source is None:
                raise HTTPException(404, "Tag not found")

            target_id = source["id"]
            if new_name is not None:
                new_norm = normalize_tag(new_name)
                cur.execute("DELETE FROM suppressed_auto_tags WHERE normalized = %s", (new_norm,))
                source_is_auto = int(source["auto_count"] or 0) > 0
                if source_is_auto and new_name != source["name"]:
                    raise HTTPException(400, "Folder tags cannot be renamed")

                if new_norm == source["normalized"]:
                    if new_name != source["name"]:
                        cur.execute(
                            "UPDATE tags SET name = %s, user_defined = true WHERE id = %s",
                            (new_name, source["id"]),
                        )
                    elif not source_is_auto:
                        cur.execute("UPDATE tags SET user_defined = true WHERE id = %s", (source["id"],))
                    final_norm = new_norm
                else:
                    if source_is_auto:
                        raise HTTPException(400, "Folder tags cannot be renamed")
                    cur.execute(
                        """
                        SELECT
                            t.id,
                            t.name,
                            t.normalized,
                            COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                        FROM tags t
                        LEFT JOIN image_tags it ON it.tag_id = t.id
                        WHERE t.normalized = %s
                        GROUP BY t.id
                        """,
                        (new_norm,),
                    )
                    target = cur.fetchone()
                    if target is not None:
                        if int(target["auto_count"] or 0) > 0:
                            raise HTTPException(400, "Cannot merge into a folder tag")
                        cur.execute(
                            """
                            INSERT INTO image_tags (image_id, tag_id, kind, created_at)
                            SELECT image_id, %s, kind, created_at
                            FROM image_tags
                            WHERE tag_id = %s
                            ON CONFLICT DO NOTHING
                            """,
                            (target["id"], source["id"]),
                        )
                        cur.execute("DELETE FROM tags WHERE id = %s", (source["id"],))
                        cur.execute("UPDATE tags SET user_defined = true WHERE id = %s", (target["id"],))
                        target_id = target["id"]
                        final_norm = target["normalized"]
                    else:
                        cur.execute(
                            "UPDATE tags SET name = %s, normalized = %s, user_defined = true WHERE id = %s",
                            (new_name, new_norm, source["id"]),
                        )
                        target_id = source["id"]
                        final_norm = new_norm

            if has_color:
                cur.execute("UPDATE tags SET color = %s WHERE id = %s", (new_color, target_id))

    summary = tag_summary_by_norm(final_norm)
    if summary is None:
        raise HTTPException(404, "Tag not found")
    return summary


def delete_tag_definition(tag: str) -> None:
    ensure_db_ready()
    norm = normalize_tag(tag)
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.name,
                    t.normalized,
                    COUNT(DISTINCT CASE WHEN it.kind = 'auto' THEN it.image_id END) AS auto_count
                FROM tags t
                LEFT JOIN image_tags it ON it.tag_id = t.id
                WHERE t.normalized = %s
                GROUP BY t.id
                """,
                (norm,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "Tag not found")
            if int(row["auto_count"] or 0) > 0:
                cur.execute(
                    """
                    INSERT INTO suppressed_auto_tags (normalized, name)
                    VALUES (%s, %s)
                    ON CONFLICT (normalized) DO UPDATE SET name = EXCLUDED.name
                    """,
                    (row["normalized"], row["name"]),
                )
            cur.execute("DELETE FROM tags WHERE id = %s", (row["id"],))


def replace_image_user_tags(image_id: str, tags: list[str]) -> None:
    ensure_db_ready()
    with db_connect() as conn:
        with conn.cursor() as cur:
            replace_image_tags(cur, image_id, tags, "user")


def replace_image_tags(cur, image_id: str, tags: list[str], kind: str) -> None:
    cleaned = clean_tag_list(tags)
    if kind == "auto":
        cleaned = filter_suppressed_auto_tags(cur, cleaned)
    cur.execute("DELETE FROM image_tags WHERE image_id = %s AND kind = %s", (image_id, kind))
    for tag in cleaned:
        tag_id = ensure_tag(cur, tag, user_defined=kind == "user")
        if tag_id is None:
            continue
        cur.execute(
            """
            INSERT INTO image_tags (image_id, tag_id, kind)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (image_id, tag_id, kind),
        )


def load_session() -> dict[str, Any]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT root_path, root_paths, search_tags, search_mode, last_image_id, tabs, active_tab_id
                FROM app_session
                WHERE id = 1
                """
            )
            row = cur.fetchone()
    return {
        "root_path": row["root_path"] if row else None,
        "root_paths": list(row["root_paths"] or []) if row else [],
        "search_tags": list(row["search_tags"] or []) if row else [],
        "search_mode": row["search_mode"] if row else "any",
        "last_image_id": row["last_image_id"] if row else None,
        "tabs": row["tabs"] or [] if row else [],
        "active_tab_id": row["active_tab_id"] if row else None,
    }


def save_session_fields(**fields: Any) -> dict[str, Any]:
    ensure_db_ready()
    if "search_mode" in fields and fields["search_mode"] not in ("any", "all"):
        fields["search_mode"] = "any"
    if "search_tags" in fields and fields["search_tags"] is not None:
        fields["search_tags"] = [normalize_tag(t) for t in clean_tag_list(fields["search_tags"])]

    allowed = {"root_path", "root_paths", "search_tags", "search_mode", "last_image_id", "tabs", "active_tab_id"}
    updates = [(key, value) for key, value in fields.items() if key in allowed]
    if updates:
        assignments = ", ".join(f"{key} = %s" for key, _ in updates)
        values = [Jsonb(value) if key == "tabs" and Jsonb is not None else value for key, value in updates]
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_session (id)
                    VALUES (1)
                    ON CONFLICT (id) DO NOTHING
                    """
                )
                cur.execute(
                    f"""
                    UPDATE app_session
                    SET {assignments}, updated_at = now()
                    WHERE id = 1
                    """,
                    values,
                )
    return load_session()


def get_image_record(img_id: str) -> Optional[dict[str, Any]]:
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, root_path, path, thumb, size, mtime, width, height, hidden
                FROM images
                WHERE id = %s AND hidden = false
                """,
                (img_id,),
            )
            return cur.fetchone()


def _decode_cursor(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("utf-8")).decode("utf-8"))
        lower_path = str(payload.get("lower_path") or "")
        path = str(payload.get("path") or "")
        image_id = str(payload.get("id") or "")
        if not image_id:
            return None
        payload["lower_path"] = lower_path
        payload["path"] = path
        payload["id"] = image_id
        payload["sort"] = str(payload.get("sort") or "path_asc")
        payload["mtime"] = int(payload.get("mtime") or 0)
        payload["size"] = int(payload.get("size") or 0)
        return payload
    except Exception:
        return None


def _encode_cursor(sort_mode: str, row: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "sort": sort_mode,
        "lower_path": row["lower_path"],
        "path": row["path"],
        "id": row["id"],
    }
    if sort_mode.startswith("date_"):
        payload["mtime"] = int(row["mtime"] or 0)
    if sort_mode.startswith("size_"):
        payload["size"] = int(row["size"] or 0)
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _sort_order_sql(sort_mode: str) -> str:
    if sort_mode == "path_desc":
        return "lower_path DESC, path DESC, id DESC"
    if sort_mode == "date_desc":
        return "mtime DESC, lower_path, path, id"
    if sort_mode == "date_asc":
        return "mtime ASC, lower_path, path, id"
    if sort_mode == "size_desc":
        return "size DESC, lower_path, path, id"
    if sort_mode == "size_asc":
        return "size ASC, lower_path, path, id"
    return "lower_path, path, id"


def _cursor_filter(sort_mode: str, cursor_payload: Optional[dict[str, Any]]) -> tuple[Optional[str], list[Any]]:
    if cursor_payload is None:
        return None, []
    if cursor_payload.get("sort") != sort_mode:
        return None, []

    lower_path = str(cursor_payload.get("lower_path") or "")
    path = str(cursor_payload.get("path") or "")
    image_id = str(cursor_payload.get("id") or "")
    if not image_id:
        return None, []

    if sort_mode == "path_desc":
        return "(lower(i.path), i.path, i.id) < (%s, %s, %s)", [lower_path, path, image_id]
    if sort_mode == "date_desc":
        value = int(cursor_payload.get("mtime") or 0)
        return (
            "(i.mtime < %s OR (i.mtime = %s AND (lower(i.path), i.path, i.id) > (%s, %s, %s)))",
            [value, value, lower_path, path, image_id],
        )
    if sort_mode == "date_asc":
        value = int(cursor_payload.get("mtime") or 0)
        return (
            "(i.mtime > %s OR (i.mtime = %s AND (lower(i.path), i.path, i.id) > (%s, %s, %s)))",
            [value, value, lower_path, path, image_id],
        )
    if sort_mode == "size_desc":
        value = int(cursor_payload.get("size") or 0)
        return (
            "(i.size < %s OR (i.size = %s AND (lower(i.path), i.path, i.id) > (%s, %s, %s)))",
            [value, value, lower_path, path, image_id],
        )
    if sort_mode == "size_asc":
        value = int(cursor_payload.get("size") or 0)
        return (
            "(i.size > %s OR (i.size = %s AND (lower(i.path), i.path, i.id) > (%s, %s, %s)))",
            [value, value, lower_path, path, image_id],
        )
    return "(lower(i.path), i.path, i.id) > (%s, %s, %s)", [lower_path, path, image_id]


def _normalize_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    return max(1, min(int(limit), MAX_PAGE_LIMIT))


def query_images_page(
    *,
    root_path: Optional[str] = None,
    root_paths: Optional[list[str]] = None,
    tags: Optional[str] = None,
    include_tags: Optional[str] = None,
    exclude_tags: Optional[str] = None,
    match_mode: Optional[str] = None,
    mode: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    sort: Optional[str] = None,
    include_total: bool = False,
) -> dict[str, Any]:
    ensure_db_ready()
    limit_value = _normalize_limit(limit)
    roots = [str(root) for root in (root_paths or ([root_path] if root_path else [])) if root]
    roots = list(dict.fromkeys(roots))
    if not roots:
        raise HTTPException(400, "No folder set")
    sort_mode = sort or "date_desc"
    if sort_mode not in IMAGE_SORTS:
        allowed = ", ".join(sorted(IMAGE_SORTS))
        raise HTTPException(400, f"Unsupported sort. Allowed: {allowed}")

    include_source = include_tags if include_tags is not None else tags
    include_list = parse_csv_tags(include_source)
    exclude_list = parse_csv_tags(exclude_tags)
    include_list = list(dict.fromkeys(include_list))
    exclude_list = list(dict.fromkeys(exclude_list))

    resolved_mode = (match_mode if include_tags is not None else mode) or "any"
    if resolved_mode not in VALID_MATCH_MODES:
        resolved_mode = "any"

    cursor_payload = _decode_cursor(cursor)

    base_filters: list[str] = ["i.root_path = ANY(%s)", "i.hidden = false"]
    base_params: list[Any] = [roots]
    filters = list(base_filters)
    params: list[Any] = list(base_params)

    cursor_sql, cursor_params = _cursor_filter(sort_mode, cursor_payload)
    if cursor_sql is not None:
        filters.append(cursor_sql)
        params.extend(cursor_params)

    join_sql = ""
    having_clauses: list[str] = []
    having_params: list[Any] = []

    if include_list or exclude_list:
        join_sql = "LEFT JOIN image_tags it ON it.image_id = i.id LEFT JOIN tags t ON t.id = it.tag_id"

    if include_list:
        if resolved_mode == "all":
            having_clauses.append(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) = %s"
            )
            having_params.append(include_list)
            having_params.append(len(include_list))
        else:
            having_clauses.append(
                "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) > 0"
            )
            having_params.append(include_list)

    if exclude_list:
        having_clauses.append(
            "COUNT(DISTINCT CASE WHEN t.normalized = ANY(%s) THEN t.normalized END) = 0"
        )
        having_params.append(exclude_list)

    params.extend(having_params)
    where_sql = " AND ".join(filters)
    total_where_sql = " AND ".join(base_filters)
    group_by_sql = "GROUP BY i.id"
    having_sql = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

    base_from = f"FROM images i {join_sql} WHERE {where_sql}"
    total_base_from = f"FROM images i {join_sql} WHERE {total_where_sql}"

    select_filtered = f"""
        SELECT
            i.id,
            i.path,
            i.thumb,
            i.size,
            i.mtime,
            i.width,
            i.height,
            lower(i.path) AS lower_path
        {base_from}
        {group_by_sql}
        {having_sql}
    """
    select_total_filtered = f"""
        SELECT i.id
        {total_base_from}
        {group_by_sql}
        {having_sql}
    """
    order_sql = _sort_order_sql(sort_mode)

    query = f"""
        {select_filtered}
        ORDER BY {order_sql}
        LIMIT %s
    """
    query_params = [*params, limit_value + 1]
    total_params = [*base_params, *having_params]

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, query_params)
            raw_rows = cur.fetchall()
            total = None
            if include_total:
                cur.execute(
                    f"""
                    WITH filtered AS (
                        {select_total_filtered}
                    )
                    SELECT COUNT(*)::bigint AS total FROM filtered
                    """,
                    total_params,
                )
                total_row = cur.fetchone()
                total = int(total_row["total"] or 0) if total_row else 0

    has_more = len(raw_rows) > limit_value
    page_rows = raw_rows[:limit_value]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(sort_mode, last)

    rows = [
        {
            "id": row["id"],
            "path": row["path"],
            "thumb": row["thumb"],
            "size": row["size"],
            "mtime": row["mtime"],
            "width": row["width"],
            "height": row["height"],
        }
        for row in page_rows
    ]

    return {
        "rows": rows,
        "page": {
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit_value,
            "returned": len(rows),
            "total": total,
            "include_total": include_total,
            "sort": sort_mode,
        },
    }


def fetch_tags_for_image_ids(ids: list[str]) -> dict[str, dict[str, list[str]]]:
    tags_by_image: dict[str, dict[str, list[str]]] = {img_id: {"auto": [], "user": []} for img_id in ids}
    if not ids:
        return tags_by_image

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT it.image_id, it.kind, t.name
                FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = ANY(%s)
                ORDER BY lower(t.name), t.name
                """,
                (ids,),
            )
            for row in cur.fetchall():
                tags_by_image[row["image_id"]][row["kind"]].append(row["name"])
    return tags_by_image


def folder_tree_rows(root_paths: list[str]) -> list[dict[str, Any]]:
    roots = [str(root) for root in root_paths if root]
    if not roots:
        return []
    ensure_db_ready()
    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT root_path, path
                FROM images
                WHERE root_path = ANY(%s)
                  AND hidden = false
                ORDER BY root_path, lower(path), path
                """,
                (roots,),
            )
            rows = cur.fetchall()
    return [{"root_path": row["root_path"], "path": row["path"]} for row in rows]


def rows_to_images(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [row["id"] for row in rows]
    tags_by_image = fetch_tags_for_image_ids(ids)
    images: list[dict[str, Any]] = []
    for row in rows:
        auto_tags = tags_by_image[row["id"]]["auto"]
        user_tags = tags_by_image[row["id"]]["user"]
        all_tags = clean_tag_list(auto_tags + user_tags)
        height = row["height"] or 0
        width = row["width"] or 0
        aspect_ratio = (width / height) if height > 0 else 1
        images.append(
            {
                "id": row["id"],
                "path": row["path"],
                "thumb": row["thumb"],
                "thumb_url": f"/thumb-file/{row['id']}.jpg",
                "size": row["size"],
                "mtime": row["mtime"],
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
                "tags": all_tags,
                "auto_tags": auto_tags,
                "folder_tags": auto_tags,
                "user_tags": user_tags,
            }
        )
    return images


def upsert_image_row(
    cur,
    *,
    root_str: str,
    rel: str,
    thumb_rel: str,
    size: int,
    mtime: int,
    width: int,
    height: int,
    existing_id: Optional[str] = None,
) -> str:
    img_id = existing_id or uuid.uuid4().hex[:12]
    cur.execute(
        """
        INSERT INTO images (
            id, root_path, path, thumb, size, mtime, width, height, hidden
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)
        ON CONFLICT (root_path, path) DO UPDATE SET
            thumb = EXCLUDED.thumb,
            size = EXCLUDED.size,
            mtime = EXCLUDED.mtime,
            width = EXCLUDED.width,
            height = EXCLUDED.height,
            hidden = false,
            updated_at = now()
        RETURNING id
        """,
        (
            img_id,
            root_str,
            rel,
            thumb_rel,
            size,
            mtime,
            width,
            height,
        ),
    )
    row = cur.fetchone()
    return row[0] if row else img_id


def mark_images_hidden_for_root(cur, root_path: str) -> None:
    cur.execute("UPDATE images SET hidden = true, updated_at = now() WHERE root_path = %s", (root_path,))


def fetch_existing_images_map(cur, root_path: str) -> dict[str, str]:
    cur.execute("SELECT path, id FROM images WHERE root_path = %s", (root_path,))
    return {path: image_id for path, image_id in cur.fetchall()}


def list_thumb_rebuild_rows(root_paths: list[str], *, limit: Optional[int]) -> list[dict[str, Any]]:
    roots = [str(root) for root in root_paths if root]
    if not roots:
        return []
    ensure_db_ready()
    max_rows = 0 if limit is None else max(0, min(int(limit), 200000))

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT id, root_path, path, thumb, mtime
                FROM images
                WHERE root_path = ANY(%s) AND hidden = false
                ORDER BY root_path, lower(path), path
            """
            params: list[Any] = [roots]
            if max_rows > 0:
                sql += " LIMIT %s"
                params.append(max_rows)
            cur.execute(sql, params)
            return list(cur.fetchall())
