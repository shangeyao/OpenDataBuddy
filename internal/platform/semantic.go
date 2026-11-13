package platform

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"sort"
	"strings"
	"unicode"
)

const semanticDim = 256

// semanticTokens 把文本切成检索词：CJK 用相邻 2-rune 重叠二元组，ASCII 用
// 全小写单词及其 3 字符前缀。返回顺序稳定、可复现。
func semanticTokens(text string) []string {
	rs := []rune(text)
	out := []string{}
	// 中文等 CJK 文本：滑动窗口取重叠二元组（忽略非字母数字的 CJK 上下文）
	cjk := []rune{}
	for _, r := range rs {
		if unicode.Is(unicode.Han, r) || unicode.Is(unicode.Hiragana, r) ||
			unicode.Is(unicode.Katakana, r) || unicode.Is(unicode.Hangul, r) {
			cjk = append(cjk, r)
		} else {
			if len(cjk) > 1 {
				for i := 0; i+1 < len(cjk); i++ {
					out = append(out, string(cjk[i:i+2]))
				}
			} else if len(cjk) == 1 {
				out = append(out, string(cjk[0]))
			}
			cjk = cjk[:0]
		}
	}
	if len(cjk) > 1 {
		for i := 0; i+1 < len(cjk); i++ {
			out = append(out, string(cjk[i:i+2]))
		}
	} else if len(cjk) == 1 {
		out = append(out, string(cjk[0]))
	}
	// ASCII 单词：小写整词 + 3 字符前缀
	word := []rune{}
	flush := func() {
		if len(word) == 0 {
			return
		}
		w := strings.ToLower(string(word))
		out = append(out, w)
		wr := []rune(w)
		if len(wr) > 3 {
			out = append(out, string(wr[:3]))
		}
		word = word[:0]
	}
	for _, r := range rs {
		if r <= unicode.MaxASCII && (unicode.IsLetter(r) || unicode.IsDigit(r)) {
			word = append(word, r)
		} else {
			flush()
		}
	}
	flush()
	return out
}

// semanticBucket 用 sha256(token) 的前两个字节（uint16）对 256 取模做桶号。
func semanticBucket(token string) int {
	sum := sha256.Sum256([]byte(token))
	return int(uint16(sum[0])<<8|uint16(sum[1])) % semanticDim
}

// semanticVector 把词表转成 256 维稀疏向量：先用 1+log(tf) 做次线性词频
// 加权，再 L2 归一化。返回 [[bucket,weight],...]，按桶号升序保证确定性。
func semanticVector(tokens []string) [][]any {
	tf := map[int]float64{}
	for _, t := range tokens {
		tf[semanticBucket(t)] += 1
	}
	// 按桶号升序聚合，避免 Go map 随机遍历顺序导致浮点求和不确定
	buckets := make([]int, 0, len(tf))
	for b := range tf {
		buckets = append(buckets, b)
	}
	sort.Ints(buckets)
	weighted := map[int]float64{}
	norm := 0.0
	for _, b := range buckets {
		w := 1 + math.Log(tf[b])
		weighted[b] = w
		norm += w * w
	}
	if norm == 0 {
		return [][]any{}
	}
	norm = math.Sqrt(norm)
	out := [][]any{}
	for _, b := range buckets {
		out = append(out, []any{b, weighted[b] / norm})
	}
	return out
}

// semanticHash 用标题+正文做 sha256 十六进制摘要，作为内容变更判据。
func semanticHash(title, content string) string {
	sum := sha256.Sum256([]byte(title + content))
	return hex.EncodeToString(sum[:])
}

// semanticParseVector 读取 doc_embeddings.vector 的 JSON 稀疏向量。
func semanticParseVector(raw string) map[int]float64 {
	var pairs [][]float64
	if json.Unmarshal([]byte(str(raw)), &pairs) != nil {
		return map[int]float64{}
	}
	out := map[int]float64{}
	for _, p := range pairs {
		if len(p) == 2 {
			out[int(p[0])] = p[1]
		}
	}
	return out
}

// semanticCosine 计算两个稀疏向量的点积（两者均已 L2 归一化），
// 按桶号升序累加以保证浮点结果确定。
func semanticCosine(q, d map[int]float64) float64 {
	if len(q) == 0 || len(d) == 0 {
		return 0
	}
	keys := make([]int, 0, len(q))
	for k := range q {
		if _, ok := d[k]; ok {
			keys = append(keys, k)
		}
	}
	sort.Ints(keys)
	s := 0.0
	for _, k := range keys {
		s += q[k] * d[k]
	}
	return s
}

// semanticDedup 返回去重后的词表（保持首次出现顺序）。
func semanticDedup(tokens []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, t := range tokens {
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	return out
}

// semanticDocCorpus 读取某工作空间的全部知识文档及其词表、df、向量与倒排权重。
// 所有计算在内存完成，不引入外部模型或网络。
type semanticDoc struct {
	row     M
	terms   []string
	vector  [][]any
	weights map[string]float64
}

func semanticBuildCorpus(docs []M) []semanticDoc {
	corpus := make([]semanticDoc, 0, len(docs))
	// 一次扫描统计文档频率 df
	df := map[string]int{}
	for _, d := range docs {
		tokens := semanticTokens(str(d["title"]) + " " + str(d["content"]))
		for _, t := range semanticDedup(tokens) {
			df[t]++
		}
		corpus = append(corpus, semanticDoc{row: d, terms: tokens, vector: semanticVector(tokens)})
	}
	N := len(docs)
	for i := range corpus {
		// 逐文档词频
		tf := map[string]int{}
		for _, t := range corpus[i].terms {
			tf[t]++
		}
		w := map[string]float64{}
		for t, n := range tf {
			idf := 1 + math.Log(float64(N)/float64(1+df[t]))
			w[t] = float64(n) * idf
		}
		corpus[i].weights = w
	}
	return corpus
}

// rebuildSemanticIndex 全量重建某工作空间的语义索引：一次扫描统计 df，
// 再逐文档 UPSERT 向量与倒排词条，最后清理已删除文档的残留行。
func (a *App) rebuildSemanticIndex(ws string) M {
	docs := a.rows("SELECT * FROM knowledge_docs WHERE workspace_id=?", ws)
	corpus := semanticBuildCorpus(docs)

	// 删除索引中已不存在的文档（先收集，用 ? 占位符逐条删）
	live := map[string]bool{}
	for _, d := range docs {
		live[str(d["id"])] = true
	}
	for _, r := range a.rows("SELECT doc_id FROM doc_embeddings WHERE workspace_id=?", ws) {
		if !live[str(r["doc_id"])] {
			a.exec("DELETE FROM doc_embeddings WHERE doc_id=?", str(r["doc_id"]))
			a.exec("DELETE FROM doc_terms WHERE workspace_id=? AND doc_id=?", ws, str(r["doc_id"]))
		}
	}
	for _, r := range a.rows("SELECT DISTINCT doc_id FROM doc_terms WHERE workspace_id=?", ws) {
		if !live[str(r["doc_id"])] {
			a.exec("DELETE FROM doc_terms WHERE workspace_id=? AND doc_id=?", ws, str(r["doc_id"]))
		}
	}

	termsTotal := 0
	for _, c := range corpus {
		docID := str(c.row["id"])
		hash := semanticHash(str(c.row["title"]), str(c.row["content"]))
		a.exec("INSERT INTO doc_embeddings (doc_id, workspace_id, vector, dim, indexed_at, content_hash) VALUES (?,?,?,?,?,?) "+
			"ON CONFLICT(doc_id) DO UPDATE SET workspace_id=excluded.workspace_id, vector=excluded.vector, dim=excluded.dim, indexed_at=excluded.indexed_at, content_hash=excluded.content_hash",
			docID, ws, js(c.vector), semanticDim, now(), hash)
		// 倒排词条：先清掉该文档旧词，避免改稿后遗留过期 term
		a.exec("DELETE FROM doc_terms WHERE workspace_id=? AND doc_id=?", ws, docID)
		for _, t := range semanticDedup(c.terms) {
			a.exec("INSERT INTO doc_terms (workspace_id, term, doc_id, weight) VALUES (?,?,?,?) "+
				"ON CONFLICT(workspace_id,term,doc_id) DO UPDATE SET weight=excluded.weight",
				ws, t, docID, c.weights[t])
			termsTotal++
		}
	}
	return M{"docs": len(docs), "terms": termsTotal, "dim": semanticDim, "rebuilt": true}
}

// semanticSearch 本地混合检索：余弦相似度 + 倒排词权重，按 0.65/0.35 融合。
func (a *App) semanticSearch(ws, q string, limit int) []M {
	if strings.TrimSpace(q) == "" {
		return []M{}
	}
	// 惰性重建：索引为空时按当前语料构建一次
	if r := a.row("SELECT count(*) AS n FROM doc_embeddings WHERE workspace_id=?", ws); num(r["n"]) == 0 {
		a.rebuildSemanticIndex(ws)
	}

	qTokens := semanticDedup(semanticTokens(q))
	qVec := semanticVector(semanticTokens(q))
	qMap := semanticParseVector(js(qVec))

	// 读取向量与倒排表
	vecs := map[string]map[int]float64{}
	for _, r := range a.rows("SELECT doc_id, vector FROM doc_embeddings WHERE workspace_id=?", ws) {
		vecs[str(r["doc_id"])] = semanticParseVector(str(r["vector"]))
	}
	lex := map[string]float64{}
	for _, t := range qTokens {
		for _, r := range a.rows("SELECT doc_id, weight FROM doc_terms WHERE workspace_id=? AND term=?", ws, t) {
			lex[str(r["doc_id"])] += num(r["weight"])
		}
	}
	maxLex := 0.0
	for _, v := range lex {
		if v > maxLex {
			maxLex = v
		}
	}

	docs := a.rows("SELECT * FROM knowledge_docs WHERE workspace_id=?", ws)
	out := []M{}
	for _, d := range docs {
		docID := str(d["id"])
		cos := semanticCosine(qMap, vecs[docID])
		lv := lex[docID]
		lvNorm := 0.0
		if maxLex > 0 {
			lvNorm = lv / maxLex
		}
		score := 0.65*cos + 0.35*lvNorm
		if score <= 0 {
			continue
		}
		snippet := []rune(str(d["content"]))
		if len(snippet) > 600 {
			snippet = snippet[:600]
		}
		res := M{}
		for k, v := range d {
			res[k] = v
		}
		res["score"] = math.Round(score*10000) / 10000
		res["snippet"] = string(snippet)
		res["engine"] = "hybrid"
		res["vector_score"] = math.Round(cos*10000) / 10000
		res["lexical_score"] = math.Round(lvNorm*10000) / 10000
		out = append(out, res)
	}
	sort.Slice(out, func(i, j int) bool {
		si, sj := num(out[i]["score"]), num(out[j]["score"])
		if si == sj {
			return str(out[i]["id"]) < str(out[j]["id"]) // 稳定且确定的次序
		}
		return si > sj
	})
	if limit < 1 || limit > 20 {
		limit = 8
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out
}

// hybridKnowledge 优先使用混合语义检索，无结果时回退到关键词检索，
// 保证既有关键词查询不退化。
func (a *App) hybridKnowledge(ws, q string, limit int) []M {
	if res := a.semanticSearch(ws, q, limit); len(res) > 0 {
		return res
	}
	return a.searchKnowledge(ws, q, limit)
}

// indexKnowledgeDoc 索引/更新单篇文档（同全量逻辑，范围收窄到一篇）。
// 文档不存在时安全地清空该 doc 的旧索引并直接返回。
func (a *App) indexKnowledgeDoc(ws, docID string) {
	d := a.row("SELECT * FROM knowledge_docs WHERE workspace_id=? AND id=?", ws, docID)
	if d == nil {
		// 清理可能残留的孤立索引行
		a.exec("DELETE FROM doc_embeddings WHERE workspace_id=? AND doc_id=?", ws, docID)
		a.exec("DELETE FROM doc_terms WHERE workspace_id=? AND doc_id=?", ws, docID)
		return
	}
	// 单篇语料下 df 均为 1；为保持与全量索引一致的 idf，使用当前空间的文档数 N
	docs := a.rows("SELECT * FROM knowledge_docs WHERE workspace_id=?", ws)
	corpus := semanticBuildCorpus(docs)
	var c semanticDoc
	for _, item := range corpus {
		if str(item.row["id"]) == docID {
			c = item
			break
		}
	}
	hash := semanticHash(str(d["title"]), str(d["content"]))
	a.exec("INSERT INTO doc_embeddings (doc_id, workspace_id, vector, dim, indexed_at, content_hash) VALUES (?,?,?,?,?,?) "+
		"ON CONFLICT(doc_id) DO UPDATE SET workspace_id=excluded.workspace_id, vector=excluded.vector, dim=excluded.dim, indexed_at=excluded.indexed_at, content_hash=excluded.content_hash",
		docID, ws, js(c.vector), semanticDim, now(), hash)
	// 先删除该文档旧的倒排词条，避免改稿后遗留过期 term
	a.exec("DELETE FROM doc_terms WHERE workspace_id=? AND doc_id=?", ws, docID)
	for _, t := range semanticDedup(c.terms) {
		a.exec("INSERT INTO doc_terms (workspace_id, term, doc_id, weight) VALUES (?,?,?,?) "+
			"ON CONFLICT(workspace_id,term,doc_id) DO UPDATE SET weight=excluded.weight",
			ws, t, docID, c.weights[t])
	}
}
