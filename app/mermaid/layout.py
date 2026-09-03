"""流程圖的分層版面：純 Python 的 Sugiyama 式實作（model.LayoutEngine）。

一律先當成 TB 算，最後才依方向轉換（LR 對調 x/y、BT 翻 y、RL 對調後翻 x；LR/RL 的尺寸進來時先對調）：

    1. 去環        DFS 找到的回邊反向；畫的時候點序倒回來，箭頭仍照原本的方向。
    2. 分層        最長路徑：沒有前驅的節點在第 0 層。
    3. 假節點      跨超過一層的邊每經過一層插一個假節點，線才有地方走、也一起參與排序。
    4. 交叉最小化  barycenter 往下、往上各掃 4 輪，留交叉數最少的那一輪。同一子圖的成員
                   在層內永遠相鄰：排序鍵先放每一層子圖的平均 barycenter，最後才是自己的。
    5. 座標        層內由左到右排開，再做 4 輪「往上下鄰居的平均 x 靠攏」的放鬆；每輪之後把
                   被子圖框罩到的非成員推出去。層距 = rank_sep + 邊標籤高 + 子圖框的內距。
    6. 邊路徑      source 底邊中點 → 假節點中心 → target 頂邊中點；自環在右側繞一小圈；
                   同一對節點的多條邊橫向錯開。標籤放在路徑的弧長中點。
    7. 子圖框      成員（含巢狀子圖）的包圍盒外擴 subgraph_pad，頂部再留標題列。

已知限制：子圖框是成員跨所有層的聯集，推開非成員只做有限輪數，成員散得很開時兩個子圖的
框線仍可能交錯（節點不會落在別人的框裡）；邊不做避障；空子圖排在整張圖下方佔位，不會被
父子圖框住；放鬆只跑固定輪數，不是最佳解。沒有隨機、排序全是 stable，結果可重現。
"""

from __future__ import annotations

from bisect import bisect_right, insort
from collections.abc import Callable

from .model import Box, EdgeRoute, FlowLayout, Flowchart, LayoutSpacing, Point, Size, Subgraph

DUMMY_W = 8.0        # 假節點保留的寬度：線要有地方走，不然會貼著旁邊的節點；標籤落在假節點上時也拿它當餘裕
LOOP_W = 20.0        # 自環往節點右側伸出的距離
LOOP_H = 8.0         # 自環的兩端離節點中心上下各多遠
MULTI_STEP = 12.0    # 同一對節點的多條邊，相鄰兩條的橫向錯開量
SWEEPS = 4           # 交叉最小化往下 + 往上各掃幾輪
RELAX_PASSES = 4     # 座標放鬆的輪數
EXCLUDE_ROUNDS = 3   # 每輪放鬆後，把非成員推出子圖框最多試幾次
FALLBACK_SIZE = Size(40.0, 28.0)   # node_sizes 漏掉的節點
EMPTY_SUB_W = 40.0   # 空子圖佔位框的內容寬
# 假節點總數的預算。跨層的邊每層一個假節點，數量是「邊數 × 跨的層數」——
# 300 節點/600 邊的病態圖能疊出七萬多個，光版面就要 0.7 秒、而且卡在 UI
# 執行緒上。超出預算的長邊就不再插假節點、直接兩點連線：那種圖的品質
# 本來就談不上，先保住「每張圖百毫秒內」的底線。
DUMMY_BUDGET = 4000

Path = tuple[str, ...]   # 節點所在的子圖路徑（由外到內的子圖 id）；不在子圖裡就是 ()

# TB 空間的點 -> 目標方向；框拿兩個對角轉完再重組（見 _transform）
_POINT_TF: dict[str, Callable[[float, float], Point]] = {
    "LR": lambda x, y: (y, x),
    "BT": lambda x, y: (x, -y),
    "RL": lambda x, y: (-y, x),
}


class LayeredLayout:
    """model.LayoutEngine 的實作。沒有狀態，每次 layout() 各自開一個 _Build。"""

    def layout(self, chart: Flowchart, node_sizes: dict[str, Size],
               label_sizes: dict[int, Size], spacing: LayoutSpacing,
               title_sizes: dict[str, Size] | None = None) -> FlowLayout:
        return _Build(chart, node_sizes, label_sizes, spacing, title_sizes or {}).run()


def _common_len(a: Path, b: Path) -> int:
    """兩條子圖路徑共同前綴的長度。"""
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def _midpoint(pts: list[Point]) -> Point:
    """折線依弧長的中點。"""
    lens = [((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 for (x0, y0), (x1, y1) in zip(pts, pts[1:])]
    half = sum(lens) / 2
    for (x0, y0), (x1, y1), ln in zip(pts, pts[1:], lens):
        if half <= ln:
            t = half / ln if ln else 0.0
            return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        half -= ln
    return pts[-1]


class _Build:
    """一次 layout() 的全部中間狀態。節點（含假節點）一律用整數索引：真節點佔 0..n_real-1，
    假節點接在後面；每個步驟只在平行陣列（layer / w / h / group …）上追加，比用 id 查 dict 快。
    """

    def __init__(self, chart: Flowchart, node_sizes: dict[str, Size],
                 label_sizes: dict[int, Size], spacing: LayoutSpacing,
                 title_sizes: dict[str, Size] | None = None) -> None:
        self.chart, self.sp, self.label_sizes = chart, spacing, label_sizes
        self.title_sizes = title_sizes or {}
        self.direction = chart.direction
        self.swap = self.direction in ("LR", "RL")
        self.path: dict[str, Path] = {}
        self._collect_paths(chart.subgraphs, ())
        # 節點宇集：chart.nodes 的順序優先；只在邊或子圖裡出現的 id 補在後面（解析器應該不會漏，這是保險）
        endpoints = [nid for e in chart.edges for nid in (e.source, e.target)]
        self.ids = list(dict.fromkeys([*chart.nodes, *endpoints, *self.path]))
        self.index = {nid: i for i, nid in enumerate(self.ids)}
        self.n_real = len(self.ids)
        sizes = [node_sizes.get(nid, FALLBACK_SIZE) for nid in self.ids]
        self.w = [s.h if self.swap else s.w for s in sizes]
        self.h = [s.w if self.swap else s.h for s in sizes]
        self.labels_tb = {k: Size(s.h, s.w) if self.swap else s for k, s in label_sizes.items()}
        self.group: list[Path] = [self.path.get(nid, ()) for nid in self.ids]
        self.grouped = bool(self.path)
        self.layer = [0] * self.n_real
        self.loops: dict[int, list[int]] = {}          # 節點 -> 自環的邊索引
        self.uv: list[tuple[int, int] | None] = []     # 邊 -> 去環後的 (上, 下)；自環是 None
        self.rev: list[bool] = []                      # 邊 -> 是不是被反向的回邊
        self.chain: list[list[int] | None] = []        # 邊 -> 上端、各假節點、下端
        self.empty_subs: list[str] = []

    def _collect_paths(self, subs: list[Subgraph], prefix: Path) -> None:
        for s in subs:
            for nid in s.nodes:
                self.path[nid] = prefix + (s.id,)
            self._collect_paths(s.children, prefix + (s.id,))

    def run(self) -> FlowLayout:
        if not self.ids:
            # 只有空子圖、一個節點都沒有的圖：還是要畫出佔位框與標題，
            # 不然使用者看到的是一張看不見的小空圖，以為壞了
            subs: dict[str, Box] = {}
            self._sub_boxes(self.chart.subgraphs, {}, subs)
            return self._finish({}, [], subs)
        order = self._break_cycles()
        self._assign_layers(order)
        self._insert_dummies()
        layers = self._order(order)
        boxes, centers = self._place(layers)
        routes = self._routes(boxes, centers)
        self._transform(boxes, routes)
        subs: dict[str, Box] = {}
        self._sub_boxes(self.chart.subgraphs, boxes, subs)
        return self._finish(boxes, routes, subs)

    # --- 1. 去環 + 2. 分層 -------------------------------------------------
    def _break_cycles(self) -> list[int]:
        """DFS 把回邊反向（記在 uv / rev），回傳反向後的拓撲順序；用明確堆疊，遞迴會碰到直譯器上限。"""
        n = self.n_real
        out: list[list[tuple[int, int]]] = [[] for _ in range(n)]
        for k, e in enumerate(self.chart.edges):
            u, v = self.index[e.source], self.index[e.target]
            self.rev.append(False)
            self.uv.append(None if u == v else (u, v))
            if u == v:
                self.loops.setdefault(u, []).append(k)
            else:
                out[u].append((k, v))
        state = [0] * n            # 0 未訪、1 還在堆疊上、2 完成
        order: list[int] = []
        for root in range(n):
            if state[root]:
                continue
            state[root] = 1
            stack = [(root, iter(out[root]))]
            while stack:
                u, edges = stack[-1]
                for k, v in edges:
                    if state[v] == 1:      # 指向還在堆疊上的祖先 = 回邊
                        self.uv[k] = (v, u)
                        self.rev[k] = True
                    elif state[v] == 0:
                        state[v] = 1
                        stack.append((v, iter(out[v])))
                        break
                else:                      # 出邊走完了：u 完成（迭代器記得走到哪，回頭再續）
                    state[u] = 2
                    order.append(u)
                    stack.pop()
        order.reverse()
        return order

    def _assign_layers(self, order: list[int]) -> None:
        """最長路徑分層：照拓撲順序推進，每個節點的層 = 所有前驅的最大層 + 1。"""
        rank = {u: r for r, u in enumerate(order)}
        layer = self.layer
        for u, v in sorted(filter(None, self.uv), key=lambda uv: rank[uv[0]]):   # 上端先定案，下端才算得準
            if layer[v] <= layer[u]:
                layer[v] = layer[u] + 1
        self.n_layers = max(layer) + 1
        self.gap_extra = [0.0] * self.n_layers   # 層 l 與 l+1 之間為邊標籤多留的高度

    # --- 3. 假節點 ---------------------------------------------------------
    def _insert_dummies(self) -> None:
        """跨層的邊每經過一層插一個假節點，順便決定邊標籤（畫在弧長中點）要佔哪裡：跨奇數層時中點
        落在正中間那段層距裡，把層距加高；跨偶數層時中點就在正中間的假節點上，把它撐成標籤的大小。
        """
        layer, w, h, group = self.layer, self.w, self.h, self.group
        budget = DUMMY_BUDGET
        for k, uv in enumerate(self.uv):
            if uv is None:
                self.chain.append(None)
                continue
            u, v = uv
            span = layer[v] - layer[u]
            common = group[u][:_common_len(group[u], group[v])]
            nodes = [u]
            if span - 1 <= budget:
                budget -= span - 1
                for step in range(1, span):
                    nodes.append(len(layer))
                    layer.append(layer[u] + step)
                    w.append(DUMMY_W)
                    h.append(0.0)
                    group.append(common)
            # 預算用完：這條長邊不插假節點，直接兩點連線（見 DUMMY_BUDGET）
            nodes.append(v)
            self.chain.append(nodes)
            lab = self.labels_tb.get(k)
            if lab is None:
                continue
            if span % 2 or len(nodes) == 2:
                g = layer[u] + span // 2
                self.gap_extra[g] = max(self.gap_extra[g], lab.h)
            else:
                d = nodes[span // 2]
                w[d], h[d] = max(w[d], lab.w + DUMMY_W), lab.h
        self.up, self.down = [[] for _ in layer], [[] for _ in layer]
        for nodes in filter(None, self.chain):
            for a, b in zip(nodes, nodes[1:]):
                self.down[a].append(b)
                self.up[b].append(a)

    # --- 4. 交叉最小化 -----------------------------------------------------
    def _order(self, order: list[int]) -> list[list[int]]:
        """回傳每層由左到右的節點索引。起手式用 DFS 順序，相關的節點一開始就靠在一起。"""
        layers: list[list[int]] = [[] for _ in range(self.n_layers)]
        for i in order:
            layers[self.layer[i]].append(i)
        for d in range(self.n_real, len(self.layer)):
            layers[self.layer[d]].append(d)
        pos = [0] * len(self.layer)
        if self.grouped:
            self.prefixes = [tuple(g[:k] for k in range(1, len(g) + 1)) for g in self.group]
            blocks: dict[Path, list[int]] = {}
            for i, ps in enumerate(self.prefixes):
                for p in ps:
                    blocks.setdefault(p, []).append(i)
            self.blocks = sorted(blocks.items())
            for lay in layers:     # 起手式也要分群，不然一開始就沒交叉的圖不會再排序
                lay.sort(key=self._group_key(lay, {i: float(p) for p, i in enumerate(lay)}))
        for lay in layers:
            for p, i in enumerate(lay):
                pos[i] = p
        best = self._crossings(layers, pos)
        best_layers = [lay[:] for lay in layers]
        for _ in range(SWEEPS):
            if best == 0:
                break
            for downward in (True, False):
                self._sweep(layers, pos, downward)
                count = self._crossings(layers, pos)
                if count < best:
                    best, best_layers = count, [lay[:] for lay in layers]
        return best_layers

    def _sweep(self, layers: list[list[int]], pos: list[int], downward: bool) -> None:
        nbrs = self.up if downward else self.down
        for l in range(1, len(layers)) if downward else range(len(layers) - 2, -1, -1):
            lay = layers[l]
            if len(lay) < 2:
                continue
            # 對面沒有鄰居就留在原地，不然它會被丟到最左邊
            bary = {i: sum([pos[j] for j in nbrs[i]]) / len(nbrs[i]) if nbrs[i] else float(pos[i]) for i in lay}
            lay.sort(key=self._group_key(lay, bary) if self.grouped else bary.__getitem__)
            for p, i in enumerate(lay):
                pos[i] = p

    def _group_key(self, lay: list[int], bary: dict[int, float]) -> Callable[[int], tuple]:
        """子圖成員必須相鄰：排序鍵先放每一層子圖的平均 barycenter、最後才是自己的。子圖在父層眼裡
        就像一個節點，同一子圖的成員共用前綴，排完必然連在一起；每一節都是 (float, str)，長短不同也能比。
        """
        sums: dict[Path, list[float]] = {}
        for i in lay:
            for p in self.prefixes[i]:
                s = sums.setdefault(p, [0.0, 0.0])
                s[0] += bary[i]
                s[1] += 1.0
        mean = {p: s[0] / s[1] for p, s in sums.items()}
        prefixes = self.prefixes
        return lambda i: tuple((mean[p], p[-1]) for p in prefixes[i]) + ((bary[i], ""),)

    def _crossings(self, layers: list[list[int]], pos: list[int]) -> int:
        """相鄰兩層的交叉數：邊依（上端, 下端）位置排好，數下端的逆序對；同一上端的邊先排序就不會互算。"""
        total = 0
        for lay in layers[:-1]:
            seen: list[int] = []
            for u in lay:
                for pv in sorted([pos[v] for v in self.down[u]]):
                    total += len(seen) - bisect_right(seen, pv)
                    insort(seen, pv)
        return total

    # --- 5. 座標 -----------------------------------------------------------
    def _place(self, layers: list[list[int]]) -> tuple[dict[str, Box], list[Point]]:
        """算 TB 空間的座標。回傳真節點的框，以及每個索引（含假節點）的中心。"""
        sp = self.sp
        n_real, w, h, group = self.n_real, self.w, self.h, self.group
        sw = w[:]                       # 佔位寬：自環要在右邊繞，得多留位置
        for i, ks in self.loops.items():
            sw[i] += LOOP_W + MULTI_STEP * (len(ks) - 1) + 4
        # 相鄰節點的最小間距：假節點之間可以擠一點；跨子圖邊界時每層框都要留內距（LR/RL 的標題列轉到層內左側）
        self.pad_left = sp.subgraph_pad + (sp.subgraph_title_h if self.swap else 0.0)
        gaps: list[list[float]] = []
        for lay in layers:
            row: list[float] = []
            for a, b in zip(lay, lay[1:]):
                gap = sp.node_sep if (a < n_real or b < n_real) else sp.node_sep / 2
                c = _common_len(group[a], group[b])
                row.append(gap + (len(group[a]) - c) * sp.subgraph_pad + (len(group[b]) - c) * self.pad_left)
            gaps.append(row)
        widths = [sum([sw[i] for i in lay]) + sum(row) for lay, row in zip(layers, gaps)]
        widest = max(widths)
        cx = [0.0] * len(w)
        for lay, row, lw in zip(layers, gaps, widths):
            x = (widest - lw) / 2       # 先每層置中於最寬的那層，放鬆從對稱的起點開始
            for p, i in enumerate(lay):
                cx[i] = x + sw[i] / 2
                x += sw[i] + (row[p] if p < len(row) else 0.0)
        self._relax(layers, gaps, sw, cx)
        tops, heights = self._layer_tops(layers)
        centers: list[Point] = [(0.0, 0.0)] * len(w)
        boxes: dict[str, Box] = {}
        for lay, top, height in zip(layers, tops, heights):
            mid = top + height / 2
            for i in lay:
                centers[i] = (cx[i], mid)
                if i < n_real:
                    boxes[self.ids[i]] = Box(cx[i] - w[i] / 2, mid - h[i] / 2, w[i], h[i])
        return boxes, centers

    def _relax(self, layers: list[list[int]], gaps: list[list[float]], sw: list[float], cx: list[float]) -> None:
        """往上下鄰居的平均 x 靠攏。一次動一個節點、只在左右鄰居留下的空間裡動，順序與間距不會壞。"""
        up, down = self.up, self.down
        n_layers = len(layers)
        for it in range(RELAX_PASSES):
            # 輪流從上、從下開始，免得一邊永遠遷就另一邊
            for l in range(n_layers) if it % 2 == 0 else range(n_layers - 1, -1, -1):
                lay, row = layers[l], gaps[l]
                for p, i in enumerate(lay):
                    ns = up[i] + down[i]
                    if not ns:
                        continue
                    want = sum([cx[j] for j in ns]) / len(ns)
                    if p < len(row):
                        want = min(want, cx[lay[p + 1]] - (sw[lay[p + 1]] + sw[i]) / 2 - row[p])
                    if p:
                        want = max(want, cx[lay[p - 1]] + (sw[lay[p - 1]] + sw[i]) / 2 + row[p - 1])
                    cx[i] = want
            if self.grouped:
                self._exclude(layers, sw, cx)

    def _exclude(self, layers: list[list[int]], sw: list[float], cx: list[float]) -> None:
        """子圖框不能罩到非成員。框是成員在所有層的聯集，別層的成員可能比這一層的更靠外，光靠層內
        間距擋不住；把被罩到的非成員連同它外側那一排整個推開。推開會讓別的框變大，所以多試幾輪。
        """
        sp, w, group, n_real = self.sp, self.w, self.group, self.n_real
        for _ in range(EXCLUDE_ROUNDS):
            moved = False
            for path, members in self.blocks:
                d = len(path)
                lo = min(cx[i] - w[i] / 2 - self.pad_left * (len(group[i]) - d + 1) for i in members)
                hi = max(cx[i] + w[i] / 2 + sp.subgraph_pad * (len(group[i]) - d + 1) for i in members)
                # 成員清單是「首次提到」的順序，跟層無關；掃描範圍必須取層的
                # 最小最大值，拿頭尾兩個成員的層當範圍會掃錯（甚至掃到空區間），
                # 非成員就會被留在框裡
                for l in range(min(self.layer[i] for i in members),
                               max(self.layer[i] for i in members) + 1):
                    lay = layers[l]
                    flags = [group[i][:d] == path for i in lay]
                    first = flags.index(True) if True in flags else -1
                    for p, i in enumerate(lay):
                        if flags[p]:
                            continue
                        sep = sp.node_sep if i < n_real else sp.node_sep / 2
                        left, right = cx[i] - sw[i] / 2 - sep, cx[i] + sw[i] / 2 + sep
                        if right <= lo or left >= hi:
                            continue
                        # 這一層有成員就照順序決定推哪邊；沒有就看它離哪邊近
                        if (p < first) if first >= 0 else (cx[i] < (lo + hi) / 2):
                            delta, span = lo - right, lay[:p + 1]
                        else:
                            delta, span = hi - left, lay[p:]
                        for j in span:
                            cx[j] += delta
                        moved = True
            if not moved:
                return

    def _layer_tops(self, layers: list[list[int]]) -> tuple[list[float], list[float]]:
        """每層的頂 y 與高。層距除了 rank_sep 與標籤高，還要為在這裡開始／結束的子圖框留內距；標題列
        在 TB 是框的上緣（進入那側）、BT 翻轉後是下緣（離開那側），LR/RL 的標題已在層內間距裡處理。
        """
        sp, h = self.sp, self.h
        heights = [max([h[i] for i in lay]) for lay in layers]
        tops = [0.0] * len(layers)
        title_top = sp.subgraph_title_h if self.direction == "TB" else 0.0
        title_bot = sp.subgraph_title_h if self.direction == "BT" else 0.0
        if self.grouped:
            # 每層出現過的所有前綴（對取前綴封閉）。節點的前綴有幾個不在對面那層 = 有幾層框在這裡開始或結束
            seen = [{p for i in lay for p in self.prefixes[i]} for lay in layers]
        for l in range(1, len(layers)):
            gap = sp.rank_sep + self.gap_extra[l - 1]
            if self.grouped:
                enter = max(sum([p not in seen[l - 1] for p in self.prefixes[i]]) for i in layers[l])
                leave = max(sum([p not in seen[l] for p in self.prefixes[i]]) for i in layers[l - 1])
                gap += enter * (sp.subgraph_pad + title_top) + leave * (sp.subgraph_pad + title_bot)
            tops[l] = tops[l - 1] + heights[l - 1] + gap
        return tops, heights

    # --- 6. 邊路徑 + 7. 標籤 -----------------------------------------------
    def _routes(self, boxes: dict[str, Box], centers: list[Point]) -> list[EdgeRoute]:
        ids, chain = self.ids, self.chain
        pairs: dict[tuple[int, int], list[int]] = {}
        for k, uv in enumerate(self.uv):
            if uv is not None:
                pairs.setdefault(uv, []).append(k)
        offset = [0.0] * len(chain)
        for ks in pairs.values():
            # 錯開的順序跟著第二個點（假節點或終點）的 x 走，扇出去的線才不會自己交叉
            ks.sort(key=lambda k: (centers[chain[k][1]][0], k))
            for j, k in enumerate(ks):
                offset[k] = (j - (len(ks) - 1) / 2) * MULTI_STEP
        routes: list[EdgeRoute] = []
        loop_seq: dict[int, int] = {}
        for k, nodes in enumerate(chain):
            if nodes is None:
                u = self.index[self.chart.edges[k].source]
                j = loop_seq[u] = loop_seq.get(u, 0) + 1
                b, ext = boxes[ids[u]], LOOP_W + MULTI_STEP * (j - 1)
                pts = [(b.right, b.cy - LOOP_H), (b.right + ext, b.cy - LOOP_H - 6),
                       (b.right + ext, b.cy + LOOP_H + 6), (b.right, b.cy + LOOP_H)]
            else:
                src, dst = boxes[ids[nodes[0]]], boxes[ids[nodes[-1]]]
                pts = [(src.cx + offset[k], src.bottom), *(centers[d] for d in nodes[1:-1]),
                       (dst.cx + offset[k], dst.y)]
                if self.rev[k]:
                    pts.reverse()
            routes.append(EdgeRoute(k, pts, _midpoint(pts) if k in self.label_sizes else None))
        return routes

    # --- 方向轉換、子圖框、留白 --------------------------------------------
    def _transform(self, boxes: dict[str, Box], routes: list[EdgeRoute]) -> None:
        """TB 座標轉成目標方向；原點會跑掉，_finish 再整體平移。"""
        tf = _POINT_TF.get(self.direction)
        if tf is None:
            return
        for b in boxes.values():
            (x0, y0), (x1, y1) = tf(b.x, b.y), tf(b.right, b.bottom)
            b.x, b.y, b.w, b.h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        for r in routes:
            r.points = [tf(x, y) for x, y in r.points]
            if r.label_center is not None:
                r.label_center = tf(*r.label_center)

    def _sub_boxes(self, subs: list[Subgraph], boxes: dict[str, Box], out: dict[str, Box]) -> list[Box]:
        """遞迴算子圖框（已是目標方向的座標），回傳這一層的框給父層當成員用。"""
        pad, title = self.sp.subgraph_pad, self.sp.subgraph_title_h
        result: list[Box] = []
        for s in subs:
            parts = [boxes[nid] for nid in s.nodes if nid in boxes] + self._sub_boxes(s.children, boxes, out)
            if not parts:
                self.empty_subs.append(s.id)
                continue
            box = Box(min(b.x for b in parts) - pad, min(b.y for b in parts) - pad - title, 0.0, 0.0)
            box.w = max(b.right for b in parts) + pad - box.x
            box.h = max(b.bottom for b in parts) + pad - box.y
            # 標題比成員寬時把框撐開（往右），否則字會畫出框外、甚至被圖片邊界裁掉。
            # 標題在 to_scene 是畫在 (box.x+8, box.y+4) 的
            lab = self.title_sizes.get(s.id)
            if lab is not None:
                box.w = max(box.w, lab.w + 16)
                box.h = max(box.h, lab.h + 8)
            out[s.id] = box
            result.append(box)
        return result

    def _finish(self, boxes: dict[str, Box], routes: list[EdgeRoute], subs: dict[str, Box]) -> FlowLayout:
        """算包圍盒、補上空子圖的佔位框、整體平移到 margin。"""
        sp = self.sp
        xs, ys = [0.0], [0.0]   # 全空（只有空子圖）時的保底原點
        for b in [*boxes.values(), *subs.values()]:
            xs += (b.x, b.right)
            ys += (b.y, b.bottom)
        for r in routes:
            xs += [x for x, _ in r.points]
            ys += [y for _, y in r.points]
            if r.label_center is not None:    # 標籤的底色框也不能被裁掉
                lab, (x, y) = self.label_sizes[r.index], r.label_center
                xs += (x - lab.w / 2, x + lab.w / 2)
                ys += (y - lab.h / 2, y + lab.h / 2)
        lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
        if self.empty_subs:
            x = lo_x
            for sid in self.empty_subs:
                lab = self.title_sizes.get(sid)
                bw = max(EMPTY_SUB_W, (lab.w + 16) if lab else 0.0) + 2 * sp.subgraph_pad
                bh = max(sp.subgraph_title_h, (lab.h + 8) if lab else 0.0) + 2 * sp.subgraph_pad
                subs[sid] = Box(x, hi_y + sp.rank_sep, bw, bh)
                x += bw + sp.node_sep
                hi_x, hi_y = max(hi_x, subs[sid].right), max(hi_y, subs[sid].bottom)
            # 佔位框列排在內容下方；hi_y 更新在迴圈裡，多個佔位框同列
            hi_y = max(b.bottom for b in subs.values())
        dx, dy = sp.margin - lo_x, sp.margin - lo_y
        for b in [*boxes.values(), *subs.values()]:
            b.x += dx
            b.y += dy
        for r in routes:
            r.points = [(x + dx, y + dy) for x, y in r.points]
            if r.label_center is not None:
                r.label_center = (r.label_center[0] + dx, r.label_center[1] + dy)
        return FlowLayout(hi_x - lo_x + 2 * sp.margin, hi_y - lo_y + 2 * sp.margin, boxes, routes, subs)
