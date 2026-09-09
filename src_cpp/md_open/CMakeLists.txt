# 原生轉交器的建置定義 —— 旗標的【單一事實來源】。
#
# 三個入口都走這裡，不再各自複製 g++ 指令列：
#   - VS Code / Cursor 的 CMake Tools 按鈕（讀 CMakePresets.json）
#   - build.ps1 的第 5 步
#   - 手動：cmake --preset ucrt64-release && cmake --build ../../build/cmake/release
#
# 工具鏈（編譯器、windres、make 的絕對路徑）釘在 CMakePresets.json，
# 這裡只描述「怎麼編」，不描述「用什麼編」。

cmake_minimum_required(VERSION 3.20)

project(MarkdownOpen LANGUAGES CXX RC)

# 【版本號從 app/__init__.py 讀，不在這裡抄第二份】
# 以前 app.rc 把版本寫死，升版時沒人記得改——1.1.0 的安裝檔裡就裝著一個標示
# 1.0.0.0 的轉交器。改成在這裡讀出來產生 version.h，三個建置入口（VS Code 的
# CMake 按鈕、build.ps1 第 5 步、手動 cmake）因此都拿到同一個版本。
set(_init_py "${CMAKE_CURRENT_SOURCE_DIR}/../../app/__init__.py")
file(READ "${_init_py}" _init_text)
if(NOT _init_text MATCHES "__version__[ \t]*=[ \t]*\"([0-9]+)\\.([0-9]+)\\.([0-9]+)\"")
    message(FATAL_ERROR "app/__init__.py 找不到純數字三段的 __version__（例如 1.0.0）")
endif()
set(APP_VERSION_CSV "${CMAKE_MATCH_1},${CMAKE_MATCH_2},${CMAKE_MATCH_3},0")
set(APP_VERSION_STR "${CMAKE_MATCH_1}.${CMAKE_MATCH_2}.${CMAKE_MATCH_3}.0")
configure_file(version.h.in "${CMAKE_CURRENT_BINARY_DIR}/version.h" @ONLY)
# 改了 __version__ 要重新產生 version.h：把來源檔登記成 configure 的相依，
# 否則 VS Code 那條路（不會每次重跑 configure）會沿用舊版本悄悄出貨。
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_init_py}")

# 資源檔含中文字串，windres 要明講 UTF-8，否則會用系統碼頁（cp950）解析而亂碼。
# -I 讓 app.rc 的 #include "version.h" 找得到上面產生的那一份。
set(CMAKE_RC_FLAGS "--codepage=65001 -I\"${CMAKE_CURRENT_BINARY_DIR}\"")

# 和先前 build.ps1 直接呼叫 g++ 的旗標對齊：
#   Release 用 -O2（CMake 對 GCC 的預設是 -O3，這裡刻意壓回去維持一致）
set(CMAKE_CXX_FLAGS_RELEASE "-O2 -DNDEBUG")

# WIN32 -> -mwindows（GUI 子系統，不開主控台）
add_executable(MarkdownOpen WIN32 main.cpp app.rc)

# make 不會解析 .rc 內部引用的檔案。少了這行，改了 assets/app.ico 之後
# 增量建置不會重編資源檔，轉交器會默默帶著舊圖示出貨。
set_source_files_properties(app.rc PROPERTIES
    OBJECT_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/../../assets/app.ico;${CMAKE_CURRENT_BINARY_DIR}/version.h"
)

set_target_properties(MarkdownOpen PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # OFF -> -std=c++17 而不是 gnu++17，維持和舊指令列相同
    CXX_EXTENSIONS OFF
)

target_compile_options(MarkdownOpen PRIVATE
    -finput-charset=UTF-8
    -fexec-charset=UTF-8
)

target_link_options(MarkdownOpen PRIVATE
    # wWinMain 進入點（寬字元）；少了它連結器會找 WinMain 而失敗
    -municode
    # 靜態連結 libgcc / winpthread，產物只相依系統 DLL，丟到別台機器也能跑
    -static
    # 發佈版 strip 符號；除錯版不能 strip，否則 gdb 什麼都看不到
    $<$<CONFIG:Release>:-s>
)

target_link_libraries(MarkdownOpen PRIVATE shell32 user32 kernel32)
