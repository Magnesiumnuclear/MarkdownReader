# Mermaid 測試

這份文件用來實際驗證閱讀器**內嵌的 Mermaid 子集渲染器**。用閱讀器打開它，
每一節的圖都應該直接畫出來；最後兩節是故意不支援與故意寫錯的，用來對照退路。

> 畫出來的是「語意正確、配色跟閱讀器走」的圖，不是 mermaid.live 的像素複製品。
> 深淺色主題、字級縮放、高 DPI 都會跟著閱讀器變。

---

## 1. 流程圖：方向與基本連線

```mermaid
graph TD
    A[開始] --> B{檔案存在？}
    B -- 是 --> C[讀取內容]
    B -- 否 --> D[顯示錯誤頁]
    C --> E([渲染])
    D --> E
    E --> F[結束]
```

同一張圖改成由左到右：

```mermaid
flowchart LR
    A[開始] --> B{檔案存在？}
    B -->|是| C[讀取內容]
    B -->|否| D[顯示錯誤頁]
    C --> E([渲染]) --> F[結束]
    D --> E
```

---

## 2. 流程圖：所有節點形狀

```mermaid
graph LR
    r[矩形] --- o(圓角)
    o --- s([體育場])
    s --- sub[[子程序]]
    sub --- cy[(資料庫)]
    cy --- ci((圓形))
    ci --- d{菱形}
    d --- h{{六角形}}
    h --- as>旗標]
    as --- lr[/平行四邊形/]
    lr --- ll[\反向平行四邊形\]
    ll --- t1[/梯形\]
    t1 --- t2[\倒梯形/]
```

---

## 3. 流程圖：連線種類與標籤

```mermaid
graph TD
    A --> B
    A --- C
    A -.-> D
    A -.- E
    A ==> F
    A === G
    B --x H
    B --o I
    C <--> J
    D -- 帶文字 --> K
    E -->|管線文字| L
    F -. 點線文字 .-> M
    G == 粗線文字 ==> N
```

鏈與 `&`：

```mermaid
graph LR
    a --> b --> c --> d
    x & y --> z & w
```

---

## 4. 流程圖：子圖（可巢狀）與迴圈

```mermaid
graph TD
    subgraph 前端
        UI[介面] --> State[狀態]
    end
    subgraph 後端
        API[API] --> DB[(資料庫)]
        subgraph 快取
            Redis[(Redis)]
        end
        API --> Redis
    end
    State --> API
    DB --> API
    UI --> UI
```

`style`、`classDef`、`class`、`linkStyle`、`click` 這些只影響官方外觀的敘述會被接受但忽略：

```mermaid
graph LR
    A[節點]:::big --> B
    classDef big fill:#f96
    style B fill:#bbf
    linkStyle 0 stroke:#f00
    click A "https://example.com"
```

---

## 5. 序列圖

```mermaid
sequenceDiagram
    title 開檔流程
    autonumber
    actor U as 使用者
    participant V as 閱讀器
    participant F as 檔案系統
    U->>+V: 雙擊 .md
    V->>+F: 讀取檔案
    F-->>-V: 內容
    V->>V: 轉換 Markdown
    Note right of V: 轉換結果<br>依主題快取
    alt 檔案存在
        V-->>U: 顯示內容
    else 找不到
        V-->>U: 顯示錯誤頁
    end
    loop 每次存檔
        F-)V: 檔案變動
        V->>V: 重新載入
    end
    opt 有錨點
        V->>U: 捲到錨點
    end
    par 同時
        V->>F: 監看檔案
    and
        V->>U: 更新狀態列
    end
    U-xV: 關閉視窗
    deactivate V
    Note over U,V: 結束
```

---

## 6. 這些會失敗（故意的）

以下兩節**應該**顯示成「一行標示 + 原始碼區塊」。這是預期行為，用來對照。

### 6-1. 未支援的圖表類型

```mermaid
classDiagram
    Animal <|-- Duck
    Animal : +int age
```

### 6-2. 語法錯誤（第 3 行）

```mermaid
graph TD
    A --> B
    這一行不是合法的敘述 !!!
    B --> C
```

---

## 結論

| 類型 | 結果 |
|------|------|
| flowchart / graph（TD、TB、BT、LR、RL） | ✅ 內嵌畫出，13 種節點形狀、各種連線與標籤、子圖 |
| sequenceDiagram | ✅ 內嵌畫出，參與者／訊息／啟用／便條／loop、alt、opt、par、critical、break |
| class、state、ER、gantt、pie、mindmap… | ❌ 顯示原始碼 + 「尚未內嵌支援」標示 |
| 語法錯誤 | ❌ 顯示原始碼 + 「解析失敗（第 N 行）」標示 |

圖太大（節點超過 300、連線超過 600、訊息超過 400）時也會退回顯示原始碼——
純 Python 的版面演算法在那個規模已經不划算了。
