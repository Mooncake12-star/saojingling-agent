import { useEffect, useState } from "react";
import { api } from "../api";

interface Kb {
  id: number;
  name: string;
  description: string | null;
  document_count: number;
}

interface Doc {
  id: number;
  filename: string;
  status: string;
}

interface UploadResult {
  id: number;
  filename: string;
  status: string;
}

/** 知识库管理页：列表 / 新建 / 删除 / 上传文档 / 文档列表。 */
export default function KnowledgePage() {
  const [kbs, setKbs] = useState<Kb[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const reloadList = () =>
    api<Kb[]>("/knowledge-bases")
      .then(setKbs)
      .catch((e) => setError(e instanceof Error ? e.message : "加载失败"));

  useEffect(() => {
    void reloadList();
  }, []);

  // 选中某个库后拉它的文档列表
  useEffect(() => {
    if (selected == null) {
      setDocs([]);
      return;
    }
    api<Doc[]>(`/knowledge-bases/${selected}/documents`).then(setDocs).catch(() => setDocs([]));
  }, [selected]);

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api<Kb>("/knowledge-bases", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), description: desc.trim() || null }),
      });
      setName("");
      setDesc("");
      void reloadList();
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建失败");
    } finally {
      setBusy(false);
    }
  };

  const removeKb = async (id: number) => {
    if (!confirm("删除知识库会同时删除里面的文档和向量，确定？")) return;
    await api(`/knowledge-bases/${id}`, { method: "DELETE" });
    if (selected === id) setSelected(null);
    void reloadList();
  };

  const upload = async (file: File) => {
    if (selected == null) return;
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const token = localStorage.getItem("sql_access");
      const res = await fetch(`/api/knowledge-bases/${selected}/documents`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail ?? `上传失败（${res.status}）`);
      }
      const doc = (await res.json()) as UploadResult;
      setDocs((d) => [...d, doc]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "上传失败");
    } finally {
      setBusy(false);
    }
  };

  const removeDoc = async (id: number) => {
    await api(`/documents/${id}`, { method: "DELETE" });
    setDocs((d) => d.filter((x) => x.id !== id));
    void reloadList();
  };

  return (
    <div className="kb-page">
      <section className="kb-create card">
        <h3>新建知识库</h3>
        <input placeholder="名称，如：X3 系列故障手册" value={name} onChange={(e) => setName(e.target.value)} />
        <input placeholder="描述（可选）" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <button onClick={() => void create()} disabled={busy || !name.trim()}>
          创建
        </button>
      </section>

      {error && <div className="error">{error}</div>}

      <section className="kb-list">
        <h3>我的知识库</h3>
        {kbs.length === 0 && <p className="empty">还没有知识库，先创建一个</p>}
        <ul>
          {kbs.map((kb) => (
            <li key={kb.id} className={selected === kb.id ? "active" : ""} onClick={() => setSelected(kb.id)}>
              <div>
                <strong>{kb.name}</strong>
                <span className="meta"> {kb.document_count} 个文档</span>
              </div>
              <button
                className="danger ghost"
                onClick={(e) => {
                  e.stopPropagation();
                  void removeKb(kb.id);
                }}
              >
                删除
              </button>
            </li>
          ))}
        </ul>
      </section>

      {selected != null && (
        <section className="kb-docs card">
          <h3>文档</h3>
          <input
            type="file"
            accept=".txt,.md,.pdf,.docx"
            disabled={busy}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void upload(f);
              e.target.value = "";
            }}
          />
          <p className="meta">上传后后台异步解析入库，列表可查看状态</p>
          <ul>
            {docs.map((d) => (
              <li key={d.id}>
                <span>{d.filename}</span>
                <span className="meta">{d.status}</span>
                <button className="danger ghost" onClick={() => void removeDoc(d.id)}>
                  删除
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
