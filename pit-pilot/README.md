# PIT 变异测试试点包（Java 8 + JUnit 4，离线可用）

在一个**不修改工程 pom.xml** 的前提下，用 PIT（pitest）自动做变异测试：故意把生产代码改坏一处，看测试抓不抓得出来。本试点包含一个故意埋了三类问题的示例工程、三种不改 pom 的调用方式（全部实跑验证）、以及一份可离线使用的完整依赖包。

> 本文档所有结论均来自实际运行。凡标注「未验证」的，都是没有在本试点里实测过的内容，不要当成实测结论。
>
> 实测环境：Linux（x86_64），Eclipse Temurin OpenJDK **1.8.0_512**，Apache Maven **3.9.11**。被测代码和测试全程在 JDK 8 上编译、运行。目标内网环境是 Windows，本试点在 Linux 上完成，Windows 特有的问题在「已知坑」里单独标注了验证状态。

## 目录结构

```
pit-pilot/
├── README.md 本文档
├── sample/ 示例工程（Maven 双模块，app 依赖 core）
│ ├── core/ 三类情况都在这个模块
│ │ └── src/main/java/com/example/core/
│ │ ├── AmountLimit.java 情况一：边界值漏测
│ │ ├── DiscountCalculator.java 情况二：假测试的对象
│ │ └── GradeClassifier.java 情况三：测得扎实的类
│ └── app/ OrderService，依赖 core，用于多模块演示
├── external-runner/pom.xml 方式 (b) 的独立 pom（在示例工程之外）
├── offline-manifest.tsv 离线清单（611 个文件的完整坐标表，含大小）
└── collect-offline-package.sh 离线包收集脚本
```

## 1. 示例工程：三类情况与实测结果

先证明原测试全绿（这是变异测试的前提，测试本身先红的话结果没有意义）：

```bash
cd sample
mvn clean test
# 实测：core 7 个测试 + app 2 个测试全部通过（surefire 2.12.4，JUnit 4.13.2）
```

示例工程故意把 surefire 钉在 **2.12.4**（模拟内网的老版本），JUnit 用 4.13.2。

### 情况一：边界值漏测（`AmountLimit`）

`exceedsLimit(amount)` 判断 `amount > 100`。测试只测了 10000（远超上限）和 50（远低于上限），**没测边界值 100 本身**。用方式 (a) 只跑这个类（命令见第 3 节），实测控制台输出：

```
>> Generated 3 mutations Killed 2 (67%)
```

`mutations.xml` 里存活的那一条，正是边界变异：

```
SURVIVED | ConditionalsBoundaryMutator | line 16 | changed conditional boundary
KILLED | NegateConditionalsMutator | line 16 | negated conditional
KILLED | BooleanTrueReturnValsMutator| line 16 | replaced boolean return with true
```

这正是人工抽查最容易漏的那一类：把 `>` 改成 `>=`，原有测试一条都不会红。补一条 `assertFalse(limit.exceedsLimit(100))` 即可杀死它（修复动作供参考，本试点保留漏测原样以便复现）。

### 情况二：假测试（`DiscountCalculator`）

`finalPrice(price, percentOff)` 做普通折扣运算。它的测试只断言「结果不小于 0」——一个几乎不可能失败的断言。实测：

```
>> Generated 4 mutations Killed 1 (25%)
```

存活 3 条（`mutations.xml` 摘录）：

```
SURVIVED | MathMutator | line 14 | Replaced integer multiplication with division
SURVIVED | MathMutator | line 15 | Replaced integer subtraction with addition
SURVIVED | PrimitiveReturnsMutator | line 15 | replaced int return with 0
KILLED | MathMutator | line 14 | Replaced integer division with multiplication
```

唯一被杀死的那条，是因为变异把价格算成了负数、恰好撞上这个弱断言。**结论：测试在跑、覆盖率也有，但断言形同虚设——PIT 能把这种问题直接报出来。**

### 情况三：测得扎实（`GradeClassifier`）

`classify(score)` 按 90/80/60 分档。测试把每个分档的边界值两侧都测了（90/89、80/79、60/59）。实测：

```
>> Generated 10 mutations Killed 10 (100%)
```

10 条变异（3 条边界、3 条条件取反、4 条返回值替换）全部被杀死。

## 2. 版本结论（实测，不是文档转述）

在 JDK **1.8.0_512** 上，用全坐标方式对同一个类逐个版本实跑 `mutationCoverage`，结果：

| PIT 版本 | 在 JDK 8 上运行 | 说明 |
|---|---|---|
| 1.15.8 | ✅ 可以 | 实测跑通 |
| **1.17.4** | ✅ **可以** | **实测到的最高可用版本，本试点选用它** |
| 1.18.0 | ❌ 不可以 | **分界点：从这个版本开始不能用** |
| 1.18.2 | ❌ 不可以 | 同上 |
| 1.19.6 | ❌ 不可以 | 同上 |
| 1.20.7 | ❌ 不可以 | 同上 |
| 1.30.2（当前最新） | ❌ 不可以 | 同上 |

不可用的报错原文（1.18.0 起每个版本都一样）：

```
java.lang.UnsupportedClassVersionError: org/pitest/maven/PitMojo has been compiled
by a more recent version of the Java Runtime (class file version 55.0),
this version of the Java Runtime only recognizes class file versions up to 52.0
```

即 1.18.x 起 PIT 本体是按 Java 11（class file 55）编译的，JDK 8（只认到 52）连插件都加载不了。**在 JDK 8 的机器上，PIT 最高只能用 1.17.4。**

**JUnit 4 需不需要额外插件：不需要（实测）。** 本试点全部运行只带了 JUnit 4.13.2，没有装任何 PIT 的测试框架插件，JUnit 4 的支持是 PIT 内置的。JUnit 5 才需要额外的 `pitest-junit5-plugin`——本试点**未验证** JUnit 5 路线（内网工程是 JUnit 4，不在试点范围内）。

## 3. 三种调用方式（都不改工程 pom）

以下命令都在 `sample/` 目录执行（方式 b、c 除外），PIT 版本一律 1.17.4。先执行一次 `mvn test-compile` 把类编译出来，或者像下面这样把 `test-compile` 写进同一条命令。

### 方式 (a)：命令行全坐标 + `-D` 参数

```bash
# 只跑一个类 + 它的测试类，同时输出 HTML 和 XML 报告
mvn test-compile org.pitest:pitest-maven:1.17.4:mutationCoverage \
-DtargetClasses=com.example.core.AmountLimit \
-DtargetTests=com.example.core.AmountLimitTest \
-DoutputFormats=HTML,XML

# 跑 core 模块的全部类（在 sample/ 下用 -pl 选模块）
mvn -pl core test-compile org.pitest:pitest-maven:1.17.4:mutationCoverage \
-DtargetClasses='com.example.core.*' \
-DtargetTests='com.example.core.*' \
-DoutputFormats=HTML,XML

# 在根目录一次跑全部模块
mvn test-compile org.pitest:pitest-maven:1.17.4:mutationCoverage \
-DtargetClasses='com.example.**' \
-DoutputFormats=HTML,XML
```

- 优点：零额外文件；所有参数都在命令行上，脚本/CI 里好拼、好改；升级 PIT 只改版本号一处。
- 缺点：命令长；参数多了以后靠人记容易漏，所以建议把常用命令存成脚本或记在这类文档里。
- 实测：本试点的情况一/二/三、多模块、单模块、性能对照，全部用这种方式跑通。

### 方式 (b)：工程之外的独立 pom

`pit-pilot/external-runner/pom.xml` 就在示例工程**外面**，工程本身一个字没动。在它所在目录执行：

```bash
cd external-runner
mvn test-compile org.pitest:pitest-maven:1.17.4:mutationCoverage
# 实测：>> Generated 17 mutations Killed 13 (76%)，与方式 (a) 全量跑 core 的结果一致
```

它是怎么工作的（踩过坑之后的最终做法，先说结论）：

- pitest-maven 有一个「空工程跳过」检查（源码类 `NonEmptyProjectCheck`），只看**本工程的源码根目录是否存在**，不存在就直接跳过并打 `Project has either no tests or no production code.`。所以只在独立 pom 里把被测代码作为依赖 jar 引进来是**行不通的**（实测被跳过）。
- 最终方案：独立 pom 把自己的源码根目录指向工程的源码目录（只读引用，不改工程文件），把代码编译进**它自己的** target 目录再跑 PIT。同时它必须重复声明被测模块的编译设置和依赖——示例里把 maven-compiler-plugin 钉在 3.8.1、依赖只声明 JUnit，与示例工程保持一致。
- 另一个实测到的变化：老资料里常见的 `additionalClasspath` 参数在 1.17.4 里**已经不存在**，写进 pom 只会得到一条 `Parameter 'additionalClasspath' is unknown` 警告，不要再用。

- 优点：配置固化在一个文件里，团队成员不用记长命令；参数（目标类、算子、线程）在 XML 里一目了然。
- 缺点：要跟被测模块重复维护编译设置和依赖列表；如果真实构建用了注解处理器或特殊编译参数，这边编译出的类可能和真实构建不完全一致；一次只能指向一个模块（多模块要准备多个这样的 pom 或改路径）。

### 方式 (c)：不用 Maven，`java -cp` 直跑 pitest-command-line

先一次性把命令行版需要的 jar 收齐（这一步可以用 Maven 的 dependency 插件在任意临时小 pom 里做，本试点实测共 16 个 jar）：

```bash
# 临时 pom 里声明 org.pitest:pitest-command-line:1.17.4 和 junit:junit:4.13.2 后：
mvn dependency:copy-dependencies -DoutputDirectory=./pit-lib
```

然后纯 Java 运行（classpath 必须逐个 jar 展开写全，见下方坑点）：

```bash
cd sample
PIT_CP=$(ls /path/to/pit-lib/*.jar | tr '\n' ':')
java -cp "${PIT_CP}core/target/classes:core/target/test-classes" \
org.pitest.mutationtest.commandline.MutationCoverageReport \
--reportDir=core/target/pit-reports \
--targetClasses=com.example.core.AmountLimit \
--targetTests=com.example.core.AmountLimitTest \
--sourceDirs=core/src/main/java \
--outputFormats=HTML,XML
# 实测：>> Generated 3 mutations Killed 2 (67%)，与方式 (a) 对该类的结果一致
```

- 优点：完全脱离 Maven 生命周期，适合没有 Maven 的环境、或要把 PIT 塞进自研脚本/流水线工具里；启动快（实测这条命令本身约 3 秒量级，不含编译）。
- 缺点：classpath 要自己维护（升级 PIT 要重新收集 jar 并核对）；被测工程得先用别的办法编译好。

**推荐：方式 (a)。** 试点里三种都跑通了，但 (a) 的维护成本最低、参数最透明，和内网现有的 Maven 工程天然契合；(b) 适合想把配置固化成文件又不能动 pom 的团队；(c) 留给没有 Maven 的特殊环境。

## 4. 只跑指定类/指定测试，以及多模块

**参数语法（实测）：**

- `-DtargetClasses`：要变异的类，逗号分隔，支持通配。`com.example.core.*` 命中该包下全部 3 个类；`com.example.**` 命中 core 与 app 两个包。写全名就只跑一个类。
- `-DtargetTests`：参与跑的测试类，规则同上。不写时 PIT 按命名约定自动找测试。**只想让某几个测试类参与，就把它们列在这里**——没列进去的测试类不会被 PIT 执行（后面的 NO_COVERAGE 演示就是利用这一点）。注意拼错或一个都没匹配上时，PIT 会以 `No mutations found` 报错停下；在确认是「本来就无目标」时可加 `-DfailWhenNoMutations=false` 放行（实测有效）。
- 报告默认写到每个模块自己的 `target/pit-reports/`。

**多模块（实测结论）：**

- 在根目录跑（方式 a 的第三条命令）：reactor 会逐个模块执行，**每个模块各出一份报告**；父 pom 模块会被自动跳过（日志：`Skipping project because: - Packaging is POM.`）。实测结果：core 17 个变异、app 3 个变异。
- **PIT 只变异「本模块自己」的类**：app 模块那次运行只生成了 `OrderService` 的 3 个变异，它依赖的 core 里的类不会被顺带变异。想覆盖哪个模块，就要在那个模块上跑。
- 只跑一个模块（实测两种正确姿势）：
1. 先把被依赖的模块装进本地仓库（`mvn install -DskipTests`），然后 `mvn -pl app test-compile org.pitest:... -DtargetClasses=com.example.app.OrderService...`——实测跑通（3/3）。
2. 或者干脆 `cd app` 进到模块目录里，用方式 (a) 的命令跑（同样需要依赖已 install）。
- **一个实测到的坑**：`mvn -pl app -am...` 且 `-DtargetClasses` 只写 app 的类时，这个参数会同样作用到被 `-am` 带进来的 core 模块上，core 因为「一个变异都没生成」直接失败（`No mutations found. This probably means there is an issue with either the supplied classpath or filters.`），app 随之被跳过。要用 `-am`，就加 `-DfailWhenNoMutations=false`（实测可跑通）；更推荐用上面姿势 1。

## 5. 报告怎么读

报告在每个模块的 `target/pit-reports/` 下：`index.html`（人看）和 `mutations.xml`（机器读/留档）。**注意：默认只出 HTML**——要 XML 必须显式加 `-DoutputFormats=HTML,XML`（实测：不加这个参数时目录里没有 mutations.xml）。

**四种主要状态的含义：**

| 状态 | 含义 | 你要做什么 |
|---|---|---|
| `KILLED` | 变异被某个测试抓出来了（测试变红） | 无事，这是好结果 |
| `SURVIVED` | 变异没被任何测试发现，改坏的代码照样全绿 | **重点看这个**：对应行缺测试或断言太弱 |
| `NO_COVERAGE` | 这行代码根本没有测试执行到 | 先补能跑到这行的测试，再谈断言 |
| `TIMED_OUT` | 变异让测试跑超时（多半是变异造出了死循环） | 一般按「抓住了」对待，但要留意别是测试本身就慢 |

（另有 NON_VIABLE、RUN_ERROR、MEMORY_ERROR 等异常类状态，本试点未实测遇到，不展开。）

控制台结尾的统计块也会按算子逐项列出这四种计数，扫一眼就知道分布。

**怎么从报告定位到「哪一行的哪种改动没被发现」（实测路径）：**

1. 打开 `index.html`，首页是各类的变异存活汇总；点进某个类，源码页里每一行旁标着该行变异的状态和说明。
2. 看 `mutations.xml` 更精确，每条变异是一个 `<mutation>` 元素，关键字段：
- `status` 属性：`KILLED` / `SURVIVED` / …
- `<mutatedClass>`、`<mutatedMethod>`、`<lineNumber>`：哪个类的哪一行
- `<mutator>`、`<description>`：哪种改动，例如 `org.pitest...ConditionalsBoundaryMutator` + `changed conditional boundary` 就是「把条件边界改了（`>` 变 `>=`）」
- `<killingTest>`：是哪个测试杀死它的；**SURVIVED 的这条为空**——这就是「没被发现」的直接证据
3. 以情况一为例：`AmountLimit` 第 16 行、ConditionalsBoundary、SURVIVED、killingTest 为空 → 回到源码第 16 行 `return amount > MAX;`，立刻明白缺的是边界值 100 的用例。

顺带一个实测小知识：控制台里会先逐组打出若干行 `Generated N Killed M (x%)`，最后才有一行带 mutations 字样的总计行，别把前面的分组行当总数。

**NO_COVERAGE 的实测样子**：把 `-DtargetTests` 只给 `GradeClassifierTest`、`-DtargetClasses` 给 `AmountLimit`（两个类毫无关系），AmountLimit 的 3 条变异全部报 `NO_COVERAGE`——说明这些行没有任何参与的测试覆盖到。

**TIMED_OUT 的实测样子**：试点外的一个临时演示类（while 循环里用 long 计数，Math 变异把 `i + 1` 改成 `i - 1` 造出死循环），XML 里该条为 `TIMED_OUT | MathMutator | Replaced long addition with subtraction`。附带发现：for 循环计数器的同类变异被 PIT 自带的 for-loop 过滤器滤掉了，不会生成——所以死循环型的 TIMED_OUT 多来自 while 循环和迭代器。

## 6. 运行时间与线程数（实测 + 经验）

实测环境是这台 1.5 核左右的小 VM，示例又极小（17 个变异），下面的绝对数字**只有趋势意义**，重点看相对关系（整条 mvn 命令的墙钟时间，含 Maven/JVM 启动）：

| 配置 | 变异数 | 总时间 |
|---|---|---|
| `-Dthreads=1`，默认算子集 | 17 | 11.078 s |
| `-Dthreads=4`，默认算子集 | 17 | 7.679 s |
| `-Dthreads=4`，算子缩减为 `CONDITIONALS_BOUNDARY,NEGATE_CONDITIONALS` | 8 | 6.894 s |

经验（结合实测）：

- **线程数**：不指定 `-Dthreads` 时 PIT 会按机器自动决定（实测日志里有对应的自动配置插件）。变异之间互相独立，线程数是线性提速的主要来源——本例 1→4 线程省了约三成总时间（若只看变异执行阶段，占比更高）。内网老机器上建议从 `-Dthreads=2` 起步，看 CPU/内存占用再往上加；线程开太多、每个 minion 子进程又各占一份堆内存，内存先爆就得不偿失。
- **第一次试用先缩范围**：先用 `-DtargetClasses` 只点几个核心类、用 `-Dmutators` 缩算子集跑通流程，再逐步放大。实测把算子从默认集缩到 2 个，变异数从 17 降到 8。本试点验证过的缩减命令：`-Dmutators=CONDITIONALS_BOUNDARY,NEGATE_CONDITIONALS`（只查条件类问题，正好对应内网最关心的边界值场景）。默认算子集（DEFAULTS）包含条件边界、条件取反、数学运算、返回值替换等，本试点实测出现过 ConditionalsBoundary、NegateConditionals、Math、PrimitiveReturns、BooleanTrueReturnVals、EmptyObjectReturnVals 这几类。
- PIT 有增量分析/历史文件（history）可以跳过没变的变异，**未验证**（本试点未实测，不写结论）。

## 7. 离线清单与离线使用

### 离线包里有什么

按「推荐方式 (a) 的完整流程」在**一个空的本地仓库**上实跑一遍（`mvn clean test` + 根目录 PIT 运行），把 Maven 实际下载的全部文件（含所有传递依赖、父 POM）作为离线集，再补入方式 (c) 专属的 `pitest-command-line`、`jopt-simple`（各 jar+pom）。结果：

- **文件总数：611 个**（jar 168 个、pom 443 个；pom 多是因为每个依赖的 POM 和父 POM 离线时都要在本地，Maven 读描述符要用）
- **总大小：47,034,466 字节 ≈ 44.9 MiB**
- 完整清单在 **`offline-manifest.tsv`**（每行一个文件：仓库相对路径、groupId、artifactId、version、类型、字节数），本文末尾附录有同一份表格。
- 用收集脚本打成的 zip：**39,571,040 字节 ≈ 37.7 MiB**，sha256 `8acb34a68df2dd1611efd0086e0c6e8b2c09eba5f6ac82bc9262cd0a2186159c`（该 zip 是在试点机器上生成的留存件；按交付约定不随仓库分发，用下面的脚本在目标机器上重建即可，内容以清单为准）。

### 怎么收集（在有完整本地仓库的机器上）

```bash
cd pit-pilot
./collect-offline-package.sh [源本地仓库路径] [输出目录]
# 默认源：~/.m2/repository；默认输出：./offline-package
# 产出：offline-package/repo/（按本地仓库布局摆好的 611 个文件）
# offline-package/pit-offline-repo.zip
# offline-package/sha256sums.txt（每个文件的 sha256 + zip 的 sha256）
```

脚本按 `offline-manifest.tsv` 逐个文件从源仓库拷出，缺任何一个都会报错退出，不会打出残缺包。脚本在 Linux bash 上实测通过；Windows 上可用 Git Bash / WSL 运行（**未验证**于原生 Windows 环境）。

### 怎么离线用（在内网机器上）

把 zip 解开得到 `repo/` 目录，放到任意位置，之后所有 Maven 命令统一带两个参数：

```bash
mvn -o -Dmaven.repo.local=<repo目录的绝对路径> test-compile \
org.pitest:pitest-maven:1.17.4:mutationCoverage \
-DtargetClasses=... -DtargetTests=... -DoutputFormats=HTML,XML
```

`-o` 是 Maven 的离线开关；`-Dmaven.repo.local` 把本地仓库指到离线包。方式 (b) 的独立 pom 不带这两个参数也能跑（前提是把离线包内容放进默认的 `~/.m2/repository`），带上同样可以。方式 (c) 不经过 Maven：16 个 jar 全部在离线包内，按清单路径取出即可（实测就是这样取的）。

### 离线验证结果（实测）

在一台等效的干净环境里做了全套复验：全新的示例工程副本 + 只含离线包文件的仓库 + `mvn -o`（且不给任何代理/网络配置）：

- `mvn -o clean test`：全部测试通过；
- 方式 (a) 根目录 PIT 运行：core 17/13、app 3/3，HTML+XML 报告正常生成；
- 方式 (b)：17/13，正常；
- 方式 (c)：用离线包里的 16 个 jar 运行，AmountLimit 3/2，正常。

**三种方式离线全部跑通。** 离线包覆盖的范围是：示例工程这套插件版本组合（clean 3.2.0、resources 3.3.1、compiler 3.8.1、surefire 2.12.4、pitest 1.17.4 等）。内网真实工程若用了清单之外的插件/依赖版本，需要按同样方法（空仓库实跑一遍）把缺的补进清单。

## 8. 已知坑

| 坑 | 状态 | 说明 |
|---|---|---|
| 老 surefire（2.12.4）与 PIT 共存 | ✅ 已验证无问题 | 示例就是 surefire 2.12.4 + JUnit 4.13.2，`mvn test` 与 PIT 都跑通。机制上 PIT 不借道 surefire 执行测试（它有自己的测试运行器，只从工程拿编译好的类和测试类路径），所以 surefire 版本主要影响 `mvn test` 本身。更老的 surefire 与其他 JUnit 版本的组合未逐个验证。 |
| Windows 路径长度 / classpath 过长 | ⚠️ 未验证 | 本试点在 Linux 上，无法复现 Windows 的命令行长度限制。PIT 有 `useClasspathJar` 参数（把 classpath 打成单个 jar 再启动子进程）就是为这类问题准备的，但本试点**未实测**该参数。内网在 Windows 上若遇到「命令行太长」类报错，先试这个参数，并把工程路径放短。 |
| 中文路径 | ✅ 已验证（Linux），Windows 侧未验证 | 实测：把示例放到中文目录下，本机默认 POSIX/C locale 时 Maven 启动阶段就崩（JVM 文件名编码是 ASCII，路径里的中文表示不出来，PIT 根本还没运行）；把 locale 切到 UTF-8（`LANG=C.UTF-8`）后整套 PIT 流程正常（10/10）。关键点在 JVM 的文件名编码而不是 PIT 本身。Windows 中文版的系统编码通常能表示中文路径，但本试点未在 Windows 上验证，建议内网第一次试跑就把工程放在纯英文短路径下，省掉这类变量。 |
| 测试之间有静态状态 | ⚠️ 未验证 | 本试点示例没有这类测试，未做专门验证。机理上：PIT 在子进程里按变异反复执行测试，靠静态变量跨测试传递状态的测试可能出现结果不稳定（同一变异时杀时不杀）。建议：真遇到结果抖动，先单独把可疑测试类拿出来小范围重复跑两次对比，再决定是否改造测试。 |
| Spring 上下文类测试很慢 | ⚠️ 未验证（无 Spring 示例），但排除手段已验证 | 本试点没有 Spring 工程，未实测上下文测试的耗时表现。已验证可用的排除手段是 `-DtargetTests` 正向圈定：只列要参与的测试类，上下文重型测试不列就不跑（第 5 节的 NO_COVERAGE 演示反向证明了圈定是精确生效的）。注意 PIT 是按覆盖关系挑测试的：一个慢测试如果覆盖了很多被变异的代码，它还是会被反复挑中执行。`excludedClasses` 参数本试点未验证，不写结论。 |
| PIT 子进程（minion）通信异常 | ✅ 已验证（本机特有） | 本试点机器上方式 (c) 首跑时 minion 报 `OutOfMemoryError... SafeDataInputStream.readBytes` 并以 MINION_DIED 退出，根因是这台 VM 的 localhost 解析优先 IPv6、父子进程 socket 对接不上（与本节下面「代理/IPv6」同一根源）。给 JVM 加 `-Djava.net.preferIPv4Stack=true` 后正常；方式 (c) 要用 `JAVA_TOOL_OPTIONS` 把这个参数传给所有子进程。普通内网 Windows 机器大概率没有这个问题，遇到 minion 通信类怪错时可以把这条当排查方向之一。 |
| 本机代理环境备注 | ✅ 已验证（本机特有） | 本试点 VM 经 HTTP 代理上网，且代理主机名的 IPv6 端点异常：Maven 默认下载全部失败，给 JVM 加 `-Djava.net.preferIPv4Stack=true`（MAVEN_OPTS）后正常；PIT 在 Maven 方式下会把父进程的 JVM 参数继承给 minion，所以方式 (a)/(b) 没再单独出问题。内网若是直连镜像仓库，不需要这个设置。 |

## 附录：离线清单全表

与 `offline-manifest.tsv` 内容一致（611 个文件，总计 47,034,466 字节）。路径按 groupId 的点分层级换成目录层级即得仓库相对路径，例如 `org.pitest:pitest-maven:1.17.4` 的 jar 在 `org/pitest/pitest-maven/1.17.4/pitest-maven-1.17.4.jar`。

<details>
<summary>展开完整清单（611 行）</summary>

| groupId | artifactId | version | 类型 | 大小（字节） |
|---|---|---|---|---|
| antlr | antlr | 2.7.7 | jar | 445288 |
| antlr | antlr | 2.7.7 | pom | 632 |
| aopalliance | aopalliance | 1.0 | pom | 363 |
| asm | asm-parent | 3.3.1 | pom | 4332 |
| asm | asm | 3.3.1 | pom | 266 |
| avalon-framework | avalon-framework | 4.1.3 | pom | 167 |
| backport-util-concurrent | backport-util-concurrent | 3.1 | jar | 331716 |
| backport-util-concurrent | backport-util-concurrent | 3.1 | pom | 880 |
| classworlds | classworlds | 1.1-alpha-2 | pom | 3128 |
| classworlds | classworlds | 1.1 | jar | 37854 |
| classworlds | classworlds | 1.1 | pom | 3324 |
| com.fasterxml.jackson.core | jackson-annotations | 2.17.0 | jar | 78488 |
| com.fasterxml.jackson.core | jackson-annotations | 2.17.0 | pom | 7059 |
| com.fasterxml.jackson.core | jackson-core | 2.17.0 | jar | 581556 |
| com.fasterxml.jackson.core | jackson-core | 2.17.0 | pom | 9641 |
| com.fasterxml.jackson.core | jackson-databind | 2.17.0 | jar | 1649184 |
| com.fasterxml.jackson.core | jackson-databind | 2.17.0 | pom | 21235 |
| com.fasterxml.jackson.dataformat | jackson-dataformat-xml | 2.17.0 | jar | 129172 |
| com.fasterxml.jackson.dataformat | jackson-dataformat-xml | 2.17.0 | pom | 7396 |
| com.fasterxml.jackson | jackson-base | 2.17.0 | pom | 11768 |
| com.fasterxml.jackson | jackson-bom | 2.17.0 | pom | 18694 |
| com.fasterxml.jackson | jackson-parent | 2.17 | pom | 6522 |
| com.fasterxml | oss-parent | 55 | pom | 23564 |
| com.fasterxml | oss-parent | 56 | pom | 23559 |
| com.fasterxml | oss-parent | 58 | pom | 23661 |
| com.fasterxml.woodstox | woodstox-core | 6.6.1 | jar | 1549934 |
| com.fasterxml.woodstox | woodstox-core | 6.6.1 | pom | 9593 |
| com.google.code.findbugs | jsr305 | 1.3.9 | pom | 965 |
| com.google.code.findbugs | jsr305 | 2.0.1 | pom | 965 |
| com.google.code.findbugs | jsr305 | 3.0.2 | pom | 4286 |
| com.google.collections | google-collections | 1.0 | jar | 639592 |
| com.google.collections | google-collections | 1.0 | pom | 2478 |
| com.google.errorprone | error_prone_annotations | 2.1.3 | pom | 1792 |
| com.google.errorprone | error_prone_parent | 2.1.3 | pom | 5098 |
| com.google | google | 1 | pom | 1554 |
| com.google | google | 5 | pom | 2458 |
| com.google.guava | guava-parent | 10.0.1 | pom | 1959 |
| com.google.guava | guava-parent | 25.1-android | pom | 10180 |
| com.google.guava | guava | 10.0.1 | pom | 5385 |
| com.google.guava | guava | 25.1-android | pom | 7786 |
| com.google.inject | guice-parent | 4.2.2 | pom | 16110 |
| com.google.inject | guice | 4.2.2 | pom | 10955 |
| com.google.j2objc | j2objc-annotations | 1.1 | pom | 2762 |
| com.googlecode.javaewah | JavaEWAH | 1.1.13 | jar | 166895 |
| com.googlecode.javaewah | JavaEWAH | 1.1.13 | pom | 4955 |
| com.thoughtworks.qdox | qdox | 2.0-M9 | jar | 317269 |
| com.thoughtworks.qdox | qdox | 2.0-M9 | pom | 15843 |
| com.thoughtworks.qdox | qdox | 2.0.3 | jar | 334163 |
| com.thoughtworks.qdox | qdox | 2.0.3 | pom | 17237 |
| commons-beanutils | commons-beanutils | 1.6 | pom | 2310 |
| commons-beanutils | commons-beanutils | 1.7.0 | pom | 357 |
| commons-beanutils | commons-beanutils | 1.9.4 | jar | 246918 |
| commons-beanutils | commons-beanutils | 1.9.4 | pom | 18005 |
| commons-chain | commons-chain | 1.1 | jar | 90001 |
| commons-chain | commons-chain | 1.1 | pom | 6013 |
| commons-codec | commons-codec | 1.11 | jar | 335042 |
| commons-codec | commons-codec | 1.11 | pom | 13966 |
| commons-collections | commons-collections | 2.0 | pom | 171 |
| commons-collections | commons-collections | 2.1 | pom | 3336 |
| commons-collections | commons-collections | 3.1 | pom | 6084 |
| commons-collections | commons-collections | 3.2.1 | pom | 12505 |
| commons-collections | commons-collections | 3.2.2 | jar | 588337 |
| commons-collections | commons-collections | 3.2.2 | pom | 12403 |
| commons-collections | commons-collections | 3.2 | pom | 11034 |
| commons-digester | commons-digester | 1.6 | pom | 974 |
| commons-digester | commons-digester | 1.8 | jar | 143602 |
| commons-digester | commons-digester | 1.8 | pom | 7004 |
| commons-io | commons-io | 2.11.0 | jar | 327135 |
| commons-io | commons-io | 2.11.0 | pom | 19729 |
| commons-io | commons-io | 2.16.1 | pom | 19623 |
| commons-io | commons-io | 2.5 | jar | 208700 |
| commons-io | commons-io | 2.5 | pom | 13290 |
| commons-io | commons-io | 2.6 | jar | 214788 |
| commons-io | commons-io | 2.6 | pom | 14256 |
| commons-lang | commons-lang | 2.4 | jar | 261809 |
| commons-lang | commons-lang | 2.4 | pom | 13976 |
| commons-logging | commons-logging | 1.0.3 | pom | 866 |
| commons-logging | commons-logging | 1.0 | pom | 163 |
| commons-logging | commons-logging | 1.1 | pom | 6182 |
| commons-logging | commons-logging | 1.2 | jar | 61829 |
| commons-logging | commons-logging | 1.2 | pom | 19206 |
| dom4j | dom4j | 1.1 | jar | 456913 |
| dom4j | dom4j | 1.1 | pom | 142 |
| javax.annotation | javax.annotation-api | 1.2 | jar | 26366 |
| javax.annotation | javax.annotation-api | 1.2 | pom | 13432 |
| javax.annotation | jsr250-api | 1.0 | pom | 1023 |
| javax.enterprise | cdi-api | 1.0 | pom | 1434 |
| javax.inject | javax.inject | 1 | jar | 2497 |
| javax.inject | javax.inject | 1 | pom | 612 |
| junit | junit | 3.8.1 | pom | 998 |
| junit | junit | 4.13.2 | jar | 384581 |
| junit | junit | 4.13.2 | pom | 27018 |
| log4j | log4j | 1.2.12 | pom | 145 |
| logkit | logkit | 1.0.1 | pom | 147 |
| net.bytebuddy | byte-buddy-parent | 1.14.9 | pom | 61609 |
| net.bytebuddy | byte-buddy | 1.14.9 | jar | 4210756 |
| net.bytebuddy | byte-buddy | 1.14.9 | pom | 15833 |
| net.i2p.crypto | eddsa | 0.3.0 | jar | 63292 |
| net.i2p.crypto | eddsa | 0.3.0 | pom | 6785 |
| net.java | jvnet-parent | 3 | pom | 4789 |
| net.sf.jopt-simple | jopt-simple | 4.9 | jar | 66469 |
| net.sf.jopt-simple | jopt-simple | 4.9 | pom | 13963 |
| org.antlr | stringtemplate | 3.2.1 | jar | 148627 |
| org.antlr | stringtemplate | 3.2.1 | pom | 7461 |
| org.apache | apache | 10 | pom | 14799 |
| org.apache | apache | 11 | pom | 14811 |
| org.apache | apache | 13 | pom | 13974 |
| org.apache | apache | 16 | pom | 15397 |
| org.apache | apache | 18 | pom | 15667 |
| org.apache | apache | 19 | pom | 15489 |
| org.apache | apache | 21 | pom | 17107 |
| org.apache | apache | 23 | pom | 18422 |
| org.apache | apache | 25 | pom | 20539 |
| org.apache | apache | 29 | pom | 20721 |
| org.apache | apache | 30 | pom | 23241 |
| org.apache | apache | 31 | pom | 23531 |
| org.apache | apache | 4 | pom | 4495 |
| org.apache | apache | 5 | pom | 4097 |
| org.apache | apache | 6 | pom | 12796 |
| org.apache | apache | 9 | pom | 15160 |
| org.apache.commons | commons-lang3 | 3.1 | jar | 315805 |
| org.apache.commons | commons-lang3 | 3.1 | pom | 16711 |
| org.apache.commons | commons-lang3 | 3.12.0 | jar | 587402 |
| org.apache.commons | commons-lang3 | 3.12.0 | pom | 30660 |
| org.apache.commons | commons-lang3 | 3.13.0 | pom | 30981 |
| org.apache.commons | commons-lang3 | 3.14.0 | jar | 657952 |
| org.apache.commons | commons-lang3 | 3.14.0 | pom | 30894 |
| org.apache.commons | commons-lang3 | 3.7 | pom | 27535 |
| org.apache.commons | commons-lang3 | 3.8.1 | pom | 27888 |
| org.apache.commons | commons-parent | 22 | pom | 41918 |
| org.apache.commons | commons-parent | 34 | pom | 55943 |
| org.apache.commons | commons-parent | 39 | pom | 61975 |
| org.apache.commons | commons-parent | 42 | pom | 67956 |
| org.apache.commons | commons-parent | 45 | pom | 73127 |
| org.apache.commons | commons-parent | 47 | pom | 77562 |
| org.apache.commons | commons-parent | 52 | pom | 79273 |
| org.apache.commons | commons-parent | 58 | pom | 82646 |
| org.apache.commons | commons-parent | 64 | pom | 77957 |
| org.apache.commons | commons-parent | 69 | pom | 77006 |
| org.apache.commons | commons-parent | 9 | pom | 21930 |
| org.apache.commons | commons-text | 1.11.0 | pom | 18529 |
| org.apache.commons | commons-text | 1.12.0 | jar | 251227 |
| org.apache.commons | commons-text | 1.12.0 | pom | 19840 |
| org.apache.commons | commons-text | 1.3 | pom | 14261 |
| org.apache.geronimo.genesis | genesis-default-flava | 2.0 | pom | 17810 |
| org.apache.geronimo.genesis | genesis-java5-flava | 2.0 | pom | 5483 |
| org.apache.geronimo.genesis | genesis | 2.0 | pom | 18418 |
| org.apache.httpcomponents | httpclient | 4.5.13 | jar | 780321 |
| org.apache.httpcomponents | httpclient | 4.5.13 | pom | 6609 |
| org.apache.httpcomponents | httpcomponents-client | 4.5.13 | pom | 16382 |
| org.apache.httpcomponents | httpcomponents-core | 4.4.13 | pom | 13167 |
| org.apache.httpcomponents | httpcomponents-core | 4.4.14 | pom | 13160 |
| org.apache.httpcomponents | httpcomponents-parent | 11 | pom | 34695 |
| org.apache.httpcomponents | httpcore | 4.4.13 | pom | 4968 |
| org.apache.httpcomponents | httpcore | 4.4.14 | jar | 328436 |
| org.apache.httpcomponents | httpcore | 4.4.14 | pom | 4967 |
| org.apache.maven.doxia | doxia-core | 1.11.1 | jar | 218021 |
| org.apache.maven.doxia | doxia-core | 1.11.1 | pom | 4526 |
| org.apache.maven.doxia | doxia-decoration-model | 1.11.1 | jar | 59774 |
| org.apache.maven.doxia | doxia-decoration-model | 1.11.1 | pom | 3405 |
| org.apache.maven.doxia | doxia-logging-api | 1.11.1 | jar | 11731 |
| org.apache.maven.doxia | doxia-logging-api | 1.11.1 | pom | 1582 |
| org.apache.maven.doxia | doxia-module-xhtml | 1.11.1 | jar | 17380 |
| org.apache.maven.doxia | doxia-module-xhtml | 1.11.1 | pom | 1978 |
| org.apache.maven.doxia | doxia-module-xhtml5 | 1.11.1 | jar | 17848 |
| org.apache.maven.doxia | doxia-module-xhtml5 | 1.11.1 | pom | 1984 |
| org.apache.maven.doxia | doxia-modules | 1.11.1 | pom | 2742 |
| org.apache.maven.doxia | doxia-sink-api | 1.11.1 | jar | 11610 |
| org.apache.maven.doxia | doxia-sink-api | 1.11.1 | pom | 1577 |
| org.apache.maven.doxia | doxia-site-renderer | 1.11.1 | jar | 65181 |
| org.apache.maven.doxia | doxia-site-renderer | 1.11.1 | pom | 7695 |
| org.apache.maven.doxia | doxia-sitetools | 1.11.1 | pom | 13594 |
| org.apache.maven.doxia | doxia-skin-model | 1.11.1 | jar | 16330 |
| org.apache.maven.doxia | doxia-skin-model | 1.11.1 | pom | 3038 |
| org.apache.maven.doxia | doxia | 1.11.1 | pom | 18034 |
| org.apache.maven | maven-aether-provider | 3.0 | jar | 51189 |
| org.apache.maven | maven-aether-provider | 3.0 | pom | 2470 |
| org.apache.maven | maven-artifact-manager | 2.0.9 | jar | 57835 |
| org.apache.maven | maven-artifact-manager | 2.0.9 | pom | 2700 |
| org.apache.maven | maven-artifact-manager | 2.2.1 | pom | 3103 |
| org.apache.maven | maven-artifact | 2.0.9 | jar | 88909 |
| org.apache.maven | maven-artifact | 2.0.9 | pom | 1619 |
| org.apache.maven | maven-artifact | 2.2.1 | pom | 1581 |
| org.apache.maven | maven-artifact | 3.0 | jar | 51908 |
| org.apache.maven | maven-artifact | 3.0 | pom | 1928 |
| org.apache.maven | maven-artifact | 3.1.0 | pom | 1632 |
| org.apache.maven | maven-artifact | 3.8.6 | jar | 58076 |
| org.apache.maven | maven-artifact | 3.8.6 | pom | 2394 |
| org.apache.maven | maven-builder-support | 3.8.6 | pom | 1404 |
| org.apache.maven | maven-core | 2.0.9 | jar | 159630 |
| org.apache.maven | maven-core | 2.0.9 | pom | 7785 |
| org.apache.maven | maven-core | 2.2.1 | pom | 11633 |
| org.apache.maven | maven-core | 3.0 | jar | 527040 |
| org.apache.maven | maven-core | 3.0 | pom | 6629 |
| org.apache.maven | maven-core | 3.8.6 | pom | 7577 |
| org.apache.maven | maven-error-diagnostics | 2.0.9 | jar | 13786 |
| org.apache.maven | maven-error-diagnostics | 2.0.9 | pom | 1744 |
| org.apache.maven | maven-error-diagnostics | 2.2.1 | pom | 1708 |
| org.apache.maven | maven-model-builder | 3.0 | jar | 147974 |
| org.apache.maven | maven-model-builder | 3.0 | pom | 2246 |
| org.apache.maven | maven-model-builder | 3.8.6 | pom | 3389 |
| org.apache.maven | maven-model | 2.0.9 | jar | 87322 |
| org.apache.maven | maven-model | 2.0.9 | pom | 3140 |
| org.apache.maven | maven-model | 2.2.1 | pom | 3240 |
| org.apache.maven | maven-model | 3.0 | jar | 164685 |
| org.apache.maven | maven-model | 3.0 | pom | 3892 |
| org.apache.maven | maven-model | 3.1.0 | pom | 3803 |
| org.apache.maven | maven-model | 3.8.6 | jar | 216125 |
| org.apache.maven | maven-model | 3.8.6 | pom | 2803 |
| org.apache.maven | maven-monitor | 2.0.9 | jar | 10281 |
| org.apache.maven | maven-monitor | 2.0.9 | pom | 1285 |
| org.apache.maven | maven-monitor | 2.2.1 | pom | 1257 |
| org.apache.maven | maven-parent | 11 | pom | 32409 |
| org.apache.maven | maven-parent | 13 | pom | 22634 |
| org.apache.maven | maven-parent | 15 | pom | 24003 |
| org.apache.maven | maven-parent | 21 | pom | 26358 |
| org.apache.maven | maven-parent | 22 | pom | 29743 |
| org.apache.maven | maven-parent | 23 | pom | 32586 |
| org.apache.maven | maven-parent | 30 | pom | 41321 |
| org.apache.maven | maven-parent | 33 | pom | 44155 |
| org.apache.maven | maven-parent | 34 | pom | 42838 |
| org.apache.maven | maven-parent | 35 | pom | 45258 |
| org.apache.maven | maven-parent | 39 | pom | 47971 |
| org.apache.maven | maven-parent | 41 | pom | 49873 |
| org.apache.maven | maven-parent | 8 | pom | 24133 |
| org.apache.maven | maven-plugin-api | 2.0.9 | jar | 12892 |
| org.apache.maven | maven-plugin-api | 2.0.9 | pom | 1494 |
| org.apache.maven | maven-plugin-api | 2.2.1 | pom | 1459 |
| org.apache.maven | maven-plugin-api | 3.0 | jar | 48920 |
| org.apache.maven | maven-plugin-api | 3.0 | pom | 2288 |
| org.apache.maven | maven-plugin-api | 3.1.0 | jar | 49820 |
| org.apache.maven | maven-plugin-api | 3.1.0 | pom | 3044 |
| org.apache.maven | maven-plugin-api | 3.8.6 | pom | 3038 |
| org.apache.maven | maven-plugin-descriptor | 2.0.9 | jar | 37043 |
| org.apache.maven | maven-plugin-descriptor | 2.0.9 | pom | 2075 |
| org.apache.maven | maven-plugin-descriptor | 2.2.1 | pom | 2064 |
| org.apache.maven | maven-plugin-parameter-documenter | 2.0.9 | jar | 20878 |
| org.apache.maven | maven-plugin-parameter-documenter | 2.0.9 | pom | 1964 |
| org.apache.maven | maven-plugin-parameter-documenter | 2.2.1 | pom | 1957 |
| org.apache.maven | maven-plugin-registry | 2.0.9 | jar | 29073 |
| org.apache.maven | maven-plugin-registry | 2.0.9 | pom | 1971 |
| org.apache.maven | maven-plugin-registry | 2.2.1 | pom | 1929 |
| org.apache.maven | maven-profile | 2.0.9 | jar | 35359 |
| org.apache.maven | maven-profile | 2.0.9 | pom | 2050 |
| org.apache.maven | maven-profile | 2.2.1 | pom | 2171 |
| org.apache.maven | maven-project | 2.0.9 | jar | 121780 |
| org.apache.maven | maven-project | 2.0.9 | pom | 2710 |
| org.apache.maven | maven-project | 2.2.1 | pom | 2773 |
| org.apache.maven | maven-repository-metadata | 2.0.9 | jar | 24559 |
| org.apache.maven | maven-repository-metadata | 2.0.9 | pom | 1901 |
| org.apache.maven | maven-repository-metadata | 2.2.1 | pom | 1871 |
| org.apache.maven | maven-repository-metadata | 3.0 | jar | 30082 |
| org.apache.maven | maven-repository-metadata | 3.0 | pom | 1929 |
| org.apache.maven | maven-repository-metadata | 3.8.6 | pom | 2116 |
| org.apache.maven | maven-resolver-provider | 3.8.6 | pom | 4222 |
| org.apache.maven | maven-settings-builder | 3.0 | jar | 37780 |
| org.apache.maven | maven-settings-builder | 3.0 | pom | 2220 |
| org.apache.maven | maven-settings-builder | 3.8.6 | pom | 2540 |
| org.apache.maven | maven-settings | 2.0.9 | jar | 49112 |
| org.apache.maven | maven-settings | 2.0.9 | pom | 2058 |
| org.apache.maven | maven-settings | 2.2.1 | pom | 2179 |
| org.apache.maven | maven-settings | 3.0 | jar | 46667 |
| org.apache.maven | maven-settings | 3.0 | pom | 1881 |
| org.apache.maven | maven-settings | 3.8.6 | pom | 1891 |
| org.apache.maven | maven-toolchain | 2.0.9 | jar | 37972 |
| org.apache.maven | maven-toolchain | 2.0.9 | pom | 3476 |
| org.apache.maven | maven | 2.0.9 | pom | 18907 |
| org.apache.maven | maven | 2.2.1 | pom | 22409 |
| org.apache.maven | maven | 3.0 | pom | 21885 |
| org.apache.maven | maven | 3.1.0 | pom | 21799 |
| org.apache.maven | maven | 3.8.6 | pom | 26945 |
| org.apache.maven.plugin-tools | maven-plugin-annotations | 3.1 | jar | 14023 |
| org.apache.maven.plugin-tools | maven-plugin-annotations | 3.1 | pom | 1624 |
| org.apache.maven.plugin-tools | maven-plugin-tools | 3.1 | pom | 16184 |
| org.apache.maven.plugins | maven-clean-plugin | 3.2.0 | jar | 35678 |
| org.apache.maven.plugins | maven-clean-plugin | 3.2.0 | pom | 5309 |
| org.apache.maven.plugins | maven-compiler-plugin | 3.8.1 | jar | 62119 |
| org.apache.maven.plugins | maven-compiler-plugin | 3.8.1 | pom | 12473 |
| org.apache.maven.plugins | maven-plugins | 33 | pom | 10687 |
| org.apache.maven.plugins | maven-plugins | 35 | pom | 9902 |
| org.apache.maven.plugins | maven-plugins | 39 | pom | 8092 |
| org.apache.maven.plugins | maven-resources-plugin | 3.3.1 | jar | 30951 |
| org.apache.maven.plugins | maven-resources-plugin | 3.3.1 | pom | 8158 |
| org.apache.maven.plugins | maven-surefire-plugin | 2.12.4 | jar | 30462 |
| org.apache.maven.plugins | maven-surefire-plugin | 2.12.4 | pom | 10475 |
| org.apache.maven.reporting | maven-reporting-api | 2.0.9 | jar | 10137 |
| org.apache.maven.reporting | maven-reporting-api | 2.0.9 | pom | 1802 |
| org.apache.maven.reporting | maven-reporting-api | 3.1.0 | jar | 10556 |
| org.apache.maven.reporting | maven-reporting-api | 3.1.0 | pom | 3764 |
| org.apache.maven.reporting | maven-reporting-impl | 3.1.0 | jar | 18687 |
| org.apache.maven.reporting | maven-reporting-impl | 3.1.0 | pom | 7167 |
| org.apache.maven.reporting | maven-reporting | 2.0.9 | pom | 1476 |
| org.apache.maven.resolver | maven-resolver-api | 1.6.3 | pom | 2630 |
| org.apache.maven.resolver | maven-resolver-impl | 1.6.3 | pom | 4229 |
| org.apache.maven.resolver | maven-resolver-spi | 1.6.3 | pom | 2652 |
| org.apache.maven.resolver | maven-resolver-util | 1.6.3 | pom | 2809 |
| org.apache.maven.resolver | maven-resolver | 1.6.3 | pom | 19024 |
| org.apache.maven.scm | maven-scm-api | 2.1.0 | jar | 109341 |
| org.apache.maven.scm | maven-scm-api | 2.1.0 | pom | 2167 |
| org.apache.maven.scm | maven-scm-manager-plexus | 2.1.0 | jar | 8916 |
| org.apache.maven.scm | maven-scm-manager-plexus | 2.1.0 | pom | 1850 |
| org.apache.maven.scm | maven-scm-managers | 2.1.0 | pom | 1529 |
| org.apache.maven.scm | maven-scm-provider-git-commons | 2.1.0 | jar | 36509 |
| org.apache.maven.scm | maven-scm-provider-git-commons | 2.1.0 | pom | 3373 |
| org.apache.maven.scm | maven-scm-provider-gitexe | 2.1.0 | jar | 74952 |
| org.apache.maven.scm | maven-scm-provider-gitexe | 2.1.0 | pom | 2646 |
| org.apache.maven.scm | maven-scm-provider-hg | 2.1.0 | jar | 67216 |
| org.apache.maven.scm | maven-scm-provider-hg | 2.1.0 | pom | 2904 |
| org.apache.maven.scm | maven-scm-provider-jgit | 2.1.0 | jar | 69569 |
| org.apache.maven.scm | maven-scm-provider-jgit | 2.1.0 | pom | 3937 |
| org.apache.maven.scm | maven-scm-provider-svn-commons | 2.1.0 | jar | 39616 |
| org.apache.maven.scm | maven-scm-provider-svn-commons | 2.1.0 | pom | 3431 |
| org.apache.maven.scm | maven-scm-provider-svnexe | 2.1.0 | jar | 83910 |
| org.apache.maven.scm | maven-scm-provider-svnexe | 2.1.0 | pom | 2625 |
| org.apache.maven.scm | maven-scm-providers-git | 2.1.0 | pom | 2208 |
| org.apache.maven.scm | maven-scm-providers-standard | 2.1.0 | pom | 2167 |
| org.apache.maven.scm | maven-scm-providers-svn | 2.1.0 | pom | 2181 |
| org.apache.maven.scm | maven-scm-providers | 2.1.0 | pom | 3503 |
| org.apache.maven.scm | maven-scm | 2.1.0 | pom | 20547 |
| org.apache.maven.shared | maven-common-artifact-filters | 1.3 | jar | 31059 |
| org.apache.maven.shared | maven-common-artifact-filters | 1.3 | pom | 3702 |
| org.apache.maven.shared | maven-common-artifact-filters | 3.1.1 | jar | 60844 |
| org.apache.maven.shared | maven-common-artifact-filters | 3.1.1 | pom | 5799 |
| org.apache.maven.shared | maven-filtering | 3.3.1 | jar | 54990 |
| org.apache.maven.shared | maven-filtering | 3.3.1 | pom | 6036 |
| org.apache.maven.shared | maven-shared-components | 12 | pom | 9337 |
| org.apache.maven.shared | maven-shared-components | 18 | pom | 4934 |
| org.apache.maven.shared | maven-shared-components | 19 | pom | 6359 |
| org.apache.maven.shared | maven-shared-components | 30 | pom | 4583 |
| org.apache.maven.shared | maven-shared-components | 34 | pom | 5096 |
| org.apache.maven.shared | maven-shared-components | 39 | pom | 3220 |
| org.apache.maven.shared | maven-shared-incremental | 1.1 | jar | 13538 |
| org.apache.maven.shared | maven-shared-incremental | 1.1 | pom | 4742 |
| org.apache.maven.shared | maven-shared-utils | 0.1 | pom | 4047 |
| org.apache.maven.shared | maven-shared-utils | 3.2.1 | jar | 166562 |
| org.apache.maven.shared | maven-shared-utils | 3.2.1 | pom | 5634 |
| org.apache.maven.shared | maven-shared-utils | 3.3.3 | jar | 154022 |
| org.apache.maven.shared | maven-shared-utils | 3.3.3 | pom | 5773 |
| org.apache.maven.shared | maven-shared-utils | 3.3.4 | jar | 153143 |
| org.apache.maven.shared | maven-shared-utils | 3.3.4 | pom | 5831 |
| org.apache.maven.surefire | maven-surefire-common | 2.12.4 | jar | 262628 |
| org.apache.maven.surefire | maven-surefire-common | 2.12.4 | pom | 5530 |
| org.apache.maven.surefire | maven-surefire-common | 3.2.5 | jar | 307901 |
| org.apache.maven.surefire | maven-surefire-common | 3.2.5 | pom | 6242 |
| org.apache.maven.surefire | surefire-api | 2.12.4 | jar | 117583 |
| org.apache.maven.surefire | surefire-api | 2.12.4 | pom | 2491 |
| org.apache.maven.surefire | surefire-api | 3.2.5 | jar | 170506 |
| org.apache.maven.surefire | surefire-api | 3.2.5 | pom | 3530 |
| org.apache.maven.surefire | surefire-booter | 2.12.4 | jar | 34682 |
| org.apache.maven.surefire | surefire-booter | 2.12.4 | pom | 3032 |
| org.apache.maven.surefire | surefire-booter | 3.2.5 | jar | 117600 |
| org.apache.maven.surefire | surefire-booter | 3.2.5 | pom | 4455 |
| org.apache.maven.surefire | surefire-extensions-api | 3.2.5 | jar | 25852 |
| org.apache.maven.surefire | surefire-extensions-api | 3.2.5 | pom | 3305 |
| org.apache.maven.surefire | surefire-extensions-spi | 3.2.5 | jar | 8177 |
| org.apache.maven.surefire | surefire-extensions-spi | 3.2.5 | pom | 1762 |
| org.apache.maven.surefire | surefire-junit4 | 2.12.4 | jar | 36922 |
| org.apache.maven.surefire | surefire-junit4 | 2.12.4 | pom | 2410 |
| org.apache.maven.surefire | surefire-logger-api | 3.2.5 | jar | 13588 |
| org.apache.maven.surefire | surefire-logger-api | 3.2.5 | pom | 3259 |
| org.apache.maven.surefire | surefire-providers | 2.12.4 | pom | 2348 |
| org.apache.maven.surefire | surefire-shared-utils | 3.2.5 | jar | 2438463 |
| org.apache.maven.surefire | surefire-shared-utils | 3.2.5 | pom | 4059 |
| org.apache.maven.surefire | surefire | 2.12.4 | pom | 13790 |
| org.apache.maven.surefire | surefire | 3.2.5 | pom | 21967 |
| org.apache.sshd | sshd-common | 2.7.0 | jar | 911366 |
| org.apache.sshd | sshd-common | 2.7.0 | pom | 4698 |
| org.apache.sshd | sshd-core | 2.7.0 | jar | 886655 |
| org.apache.sshd | sshd-core | 2.7.0 | pom | 9715 |
| org.apache.sshd | sshd-osgi | 2.7.0 | jar | 1749213 |
| org.apache.sshd | sshd-osgi | 2.7.0 | pom | 7606 |
| org.apache.sshd | sshd-sftp | 2.7.0 | jar | 353809 |
| org.apache.sshd | sshd-sftp | 2.7.0 | pom | 8841 |
| org.apache.sshd | sshd | 2.7.0 | pom | 75948 |
| org.apache.velocity | velocity-tools | 2.0 | jar | 346580 |
| org.apache.velocity | velocity-tools | 2.0 | pom | 18200 |
| org.apache.velocity | velocity | 1.6.2 | pom | 10620 |
| org.apache.velocity | velocity | 1.7 | jar | 449505 |
| org.apache.velocity | velocity | 1.7 | pom | 10857 |
| org.apache.xbean | xbean-reflect | 3.7 | jar | 148098 |
| org.apache.xbean | xbean-reflect | 3.7 | pom | 5092 |
| org.apache.xbean | xbean | 3.7 | pom | 15438 |
| org.checkerframework | checker-compat-qual | 2.0.0 | pom | 4205 |
| org.codehaus | codehaus-parent | 4 | pom | 4821 |
| org.codehaus.mojo | animal-sniffer-annotations | 1.14 | pom | 2540 |
| org.codehaus.mojo | animal-sniffer-parent | 1.14 | pom | 4385 |
| org.codehaus.mojo | mojo-parent | 34 | pom | 24338 |
| org.codehaus.plexus | plexus-cipher | 2.0 | pom | 4417 |
| org.codehaus.plexus | plexus-classworlds | 1.2-alpha-9 | pom | 3223 |
| org.codehaus.plexus | plexus-classworlds | 2.2.3 | jar | 46099 |
| org.codehaus.plexus | plexus-classworlds | 2.2.3 | pom | 3997 |
| org.codehaus.plexus | plexus-classworlds | 2.4 | pom | 3883 |
| org.codehaus.plexus | plexus-classworlds | 2.5.2 | pom | 7305 |
| org.codehaus.plexus | plexus-classworlds | 2.6.0 | jar | 52873 |
| org.codehaus.plexus | plexus-classworlds | 2.6.0 | pom | 7911 |
| org.codehaus.plexus | plexus-compiler-api | 2.8.4 | jar | 27080 |
| org.codehaus.plexus | plexus-compiler-api | 2.8.4 | pom | 867 |
| org.codehaus.plexus | plexus-compiler-javac | 2.8.4 | jar | 21277 |
| org.codehaus.plexus | plexus-compiler-javac | 2.8.4 | pom | 771 |
| org.codehaus.plexus | plexus-compiler-manager | 2.8.4 | jar | 4708 |
| org.codehaus.plexus | plexus-compiler-manager | 2.8.4 | pom | 692 |
| org.codehaus.plexus | plexus-compiler | 2.8.4 | pom | 6028 |
| org.codehaus.plexus | plexus-compilers | 2.8.4 | pom | 1341 |
| org.codehaus.plexus | plexus-component-annotations | 1.5.5 | pom | 815 |
| org.codehaus.plexus | plexus-component-annotations | 1.7.1 | jar | 4288 |
| org.codehaus.plexus | plexus-component-annotations | 1.7.1 | pom | 770 |
| org.codehaus.plexus | plexus-component-annotations | 2.0.0 | pom | 750 |
| org.codehaus.plexus | plexus-component-annotations | 2.1.0 | jar | 4225 |
| org.codehaus.plexus | plexus-component-annotations | 2.1.0 | pom | 750 |
| org.codehaus.plexus | plexus-components | 1.1.12 | pom | 3003 |
| org.codehaus.plexus | plexus-components | 1.1.14 | pom | 5842 |
| org.codehaus.plexus | plexus-components | 1.1.18 | pom | 5355 |
| org.codehaus.plexus | plexus-components | 1.3.1 | pom | 3064 |
| org.codehaus.plexus | plexus-components | 4.0 | pom | 2657 |
| org.codehaus.plexus | plexus-container-default | 1.0-alpha-30 | pom | 3477 |
| org.codehaus.plexus | plexus-container-default | 1.0-alpha-9-stable-1 | jar | 194185 |
| org.codehaus.plexus | plexus-container-default | 1.0-alpha-9-stable-1 | pom | 3949 |
| org.codehaus.plexus | plexus-container-default | 1.0-alpha-9 | pom | 1236 |
| org.codehaus.plexus | plexus-container-default | 2.1.0 | jar | 229979 |
| org.codehaus.plexus | plexus-container-default | 2.1.0 | pom | 2964 |
| org.codehaus.plexus | plexus-containers | 1.0-alpha-30 | pom | 1894 |
| org.codehaus.plexus | plexus-containers | 1.0.3 | pom | 492 |
| org.codehaus.plexus | plexus-containers | 1.5.5 | pom | 4240 |
| org.codehaus.plexus | plexus-containers | 1.7.1 | pom | 5032 |
| org.codehaus.plexus | plexus-containers | 2.0.0 | pom | 4805 |
| org.codehaus.plexus | plexus-containers | 2.1.0 | pom | 4805 |
| org.codehaus.plexus | plexus-i18n | 1.0-beta-10 | jar | 11931 |
| org.codehaus.plexus | plexus-i18n | 1.0-beta-10 | pom | 2075 |
| org.codehaus.plexus | plexus-interactivity-api | 1.3 | jar | 10203 |
| org.codehaus.plexus | plexus-interactivity-api | 1.3 | pom | 1675 |
| org.codehaus.plexus | plexus-interactivity | 1.3 | pom | 1868 |
| org.codehaus.plexus | plexus-interpolation | 1.11 | pom | 889 |
| org.codehaus.plexus | plexus-interpolation | 1.14 | jar | 61072 |
| org.codehaus.plexus | plexus-interpolation | 1.14 | pom | 910 |
| org.codehaus.plexus | plexus-interpolation | 1.21 | pom | 1540 |
| org.codehaus.plexus | plexus-interpolation | 1.26 | jar | 85348 |
| org.codehaus.plexus | plexus-interpolation | 1.26 | pom | 2655 |
| org.codehaus.plexus | plexus-java | 0.9.10 | jar | 39426 |
| org.codehaus.plexus | plexus-java | 0.9.10 | pom | 5108 |
| org.codehaus.plexus | plexus-java | 1.2.0 | jar | 57542 |
| org.codehaus.plexus | plexus-java | 1.2.0 | pom | 4273 |
| org.codehaus.plexus | plexus-languages | 0.9.10 | pom | 4140 |
| org.codehaus.plexus | plexus-languages | 1.2.0 | pom | 3193 |
| org.codehaus.plexus | plexus-sec-dispatcher | 2.0 | pom | 3234 |
| org.codehaus.plexus | plexus-utils | 1.0.4 | pom | 6856 |
| org.codehaus.plexus | plexus-utils | 1.4.5 | pom | 2265 |
| org.codehaus.plexus | plexus-utils | 1.5.15 | pom | 6849 |
| org.codehaus.plexus | plexus-utils | 1.5.5 | pom | 5147 |
| org.codehaus.plexus | plexus-utils | 2.0.4 | jar | 222137 |
| org.codehaus.plexus | plexus-utils | 2.0.4 | pom | 3334 |
| org.codehaus.plexus | plexus-utils | 2.0.5 | pom | 3334 |
| org.codehaus.plexus | plexus-utils | 2.1 | pom | 4034 |
| org.codehaus.plexus | plexus-utils | 3.0.10 | pom | 3144 |
| org.codehaus.plexus | plexus-utils | 3.0.22 | pom | 3831 |
| org.codehaus.plexus | plexus-utils | 3.0.24 | pom | 4127 |
| org.codehaus.plexus | plexus-utils | 3.0.8 | jar | 231822 |
| org.codehaus.plexus | plexus-utils | 3.0.8 | pom | 3142 |
| org.codehaus.plexus | plexus-utils | 3.1.1 | pom | 5069 |
| org.codehaus.plexus | plexus-utils | 3.3.0 | pom | 5182 |
| org.codehaus.plexus | plexus-utils | 3.3.1 | jar | 262026 |
| org.codehaus.plexus | plexus-utils | 3.3.1 | pom | 5342 |
| org.codehaus.plexus | plexus-utils | 3.4.1 | pom | 7963 |
| org.codehaus.plexus | plexus-utils | 3.5.0 | pom | 8012 |
| org.codehaus.plexus | plexus-utils | 3.5.1 | jar | 269335 |
| org.codehaus.plexus | plexus-utils | 3.5.1 | pom | 8768 |
| org.codehaus.plexus | plexus-utils | 4.0.0 | pom | 8656 |
| org.codehaus.plexus | plexus-velocity | 1.2 | jar | 8087 |
| org.codehaus.plexus | plexus-velocity | 1.2 | pom | 2821 |
| org.codehaus.plexus | plexus-xml | 3.0.0 | jar | 93014 |
| org.codehaus.plexus | plexus-xml | 3.0.0 | pom | 3738 |
| org.codehaus.plexus | plexus | 1.0.10 | pom | 8250 |
| org.codehaus.plexus | plexus | 1.0.11 | pom | 8969 |
| org.codehaus.plexus | plexus | 1.0.4 | pom | 5737 |
| org.codehaus.plexus | plexus | 10 | pom | 25398 |
| org.codehaus.plexus | plexus | 13 | pom | 27377 |
| org.codehaus.plexus | plexus | 15 | pom | 27795 |
| org.codehaus.plexus | plexus | 16 | pom | 27638 |
| org.codehaus.plexus | plexus | 2.0.2 | pom | 11610 |
| org.codehaus.plexus | plexus | 2.0.6 | pom | 16763 |
| org.codehaus.plexus | plexus | 2.0.7 | pom | 17295 |
| org.codehaus.plexus | plexus | 3.2 | pom | 18746 |
| org.codehaus.plexus | plexus | 3.3.1 | pom | 20446 |
| org.codehaus.plexus | plexus | 3.3 | pom | 19900 |
| org.codehaus.plexus | plexus | 4.0 | pom | 21500 |
| org.codehaus.plexus | plexus | 5.1 | pom | 22504 |
| org.codehaus.plexus | plexus | 8 | pom | 25441 |
| org.codehaus.woodstox | stax2-api | 4.2.2 | jar | 195922 |
| org.codehaus.woodstox | stax2-api | 4.2.2 | pom | 6297 |
| org.eclipse.aether | aether-api | 1.0.0.v20140518 | jar | 136299 |
| org.eclipse.aether | aether-api | 1.0.0.v20140518 | pom | 1896 |
| org.eclipse.aether | aether-util | 1.0.0.v20140518 | jar | 145829 |
| org.eclipse.aether | aether-util | 1.0.0.v20140518 | pom | 2194 |
| org.eclipse.aether | aether | 1.0.0.v20140518 | pom | 30152 |
| org.eclipse.jgit | org.eclipse.jgit-parent | 5.13.3.202401111512-r | pom | 33707 |
| org.eclipse.jgit | org.eclipse.jgit.ssh.apache | 5.13.3.202401111512-r | jar | 181668 |
| org.eclipse.jgit | org.eclipse.jgit.ssh.apache | 5.13.3.202401111512-r | pom | 7558 |
| org.eclipse.jgit | org.eclipse.jgit | 5.13.3.202401111512-r | jar | 2849142 |
| org.eclipse.jgit | org.eclipse.jgit | 5.13.3.202401111512-r | pom | 7552 |
| org.eclipse.sisu | org.eclipse.sisu.inject | 0.0.0.M2a | pom | 4969 |
| org.eclipse.sisu | org.eclipse.sisu.inject | 0.3.5 | jar | 379348 |
| org.eclipse.sisu | org.eclipse.sisu.inject | 0.3.5 | pom | 2625 |
| org.eclipse.sisu | org.eclipse.sisu.plexus | 0.0.0.M2a | pom | 5894 |
| org.eclipse.sisu | org.eclipse.sisu.plexus | 0.3.5 | jar | 205307 |
| org.eclipse.sisu | org.eclipse.sisu.plexus | 0.3.5 | pom | 4285 |
| org.eclipse.sisu | sisu-inject | 0.0.0.M2a | pom | 7807 |
| org.eclipse.sisu | sisu-inject | 0.3.5 | pom | 14407 |
| org.eclipse.sisu | sisu-plexus | 0.0.0.M2a | pom | 7915 |
| org.eclipse.sisu | sisu-plexus | 0.3.5 | pom | 13726 |
| org.hamcrest | hamcrest-core | 1.3 | jar | 45024 |
| org.hamcrest | hamcrest-core | 1.3 | pom | 766 |
| org.hamcrest | hamcrest-parent | 1.3 | pom | 1972 |
| org.jboss.weld | weld-api-bom | 1.0 | pom | 7907 |
| org.jboss.weld | weld-api-parent | 1.0 | pom | 2355 |
| org.jboss.weld | weld-parent | 6 | pom | 20692 |
| org.junit | junit-bom | 5.10.0 | pom | 5649 |
| org.junit | junit-bom | 5.10.1 | pom | 5649 |
| org.junit | junit-bom | 5.10.2 | pom | 5649 |
| org.junit | junit-bom | 5.7.1 | pom | 5098 |
| org.junit | junit-bom | 5.7.2 | pom | 5098 |
| org.junit | junit-bom | 5.9.3 | pom | 5630 |
| org.ow2.asm | asm-analysis | 9.7.1 | jar | 35126 |
| org.ow2.asm | asm-analysis | 9.7.1 | pom | 2625 |
| org.ow2.asm | asm-commons | 9.7.1 | jar | 73459 |
| org.ow2.asm | asm-commons | 9.7.1 | pom | 2794 |
| org.ow2.asm | asm-tree | 9.7.1 | jar | 51939 |
| org.ow2.asm | asm-tree | 9.7.1 | pom | 2596 |
| org.ow2.asm | asm-util | 9.7.1 | jar | 94519 |
| org.ow2.asm | asm-util | 9.7.1 | pom | 2946 |
| org.ow2.asm | asm | 6.2 | jar | 111214 |
| org.ow2.asm | asm | 6.2 | pom | 2936 |
| org.ow2.asm | asm | 9.6 | pom | 2370 |
| org.ow2.asm | asm | 9.7.1 | jar | 126093 |
| org.ow2.asm | asm | 9.7.1 | pom | 2372 |
| org.ow2 | ow2 | 1.5.1 | pom | 11290 |
| org.ow2 | ow2 | 1.5 | pom | 11230 |
| org.pitest | pitest-aggregator | 1.17.4 | jar | 2459473 |
| org.pitest | pitest-aggregator | 1.17.4 | pom | 2902 |
| org.pitest | pitest-command-line | 1.17.4 | jar | 742741 |
| org.pitest | pitest-command-line | 1.17.4 | pom | 2836 |
| org.pitest | pitest-entry | 1.17.4 | jar | 783857 |
| org.pitest | pitest-entry | 1.17.4 | pom | 5053 |
| org.pitest | pitest-html-report | 1.17.4 | jar | 658425 |
| org.pitest | pitest-html-report | 1.17.4 | pom | 3625 |
| org.pitest | pitest-maven | 1.17.4 | jar | 85872 |
| org.pitest | pitest-maven | 1.17.4 | pom | 7037 |
| org.pitest | pitest-parent | 1.17.4 | pom | 14149 |
| org.pitest | pitest | 1.17.4 | jar | 619977 |
| org.pitest | pitest | 1.17.4 | pom | 6156 |
| org.slf4j | jcl-over-slf4j | 1.5.6 | pom | 2169 |
| org.slf4j | jcl-over-slf4j | 1.7.30 | pom | 1181 |
| org.slf4j | jcl-over-slf4j | 1.7.36 | jar | 16555 |
| org.slf4j | jcl-over-slf4j | 1.7.36 | pom | 1181 |
| org.slf4j | jul-to-slf4j | 1.7.12 | jar | 4736 |
| org.slf4j | jul-to-slf4j | 1.7.12 | pom | 1963 |
| org.slf4j | slf4j-api | 1.5.6 | pom | 2984 |
| org.slf4j | slf4j-api | 1.6.0 | pom | 2994 |
| org.slf4j | slf4j-api | 1.7.12 | pom | 2661 |
| org.slf4j | slf4j-api | 1.7.30 | pom | 3835 |
| org.slf4j | slf4j-api | 1.7.36 | jar | 41125 |
| org.slf4j | slf4j-api | 1.7.36 | pom | 2743 |
| org.slf4j | slf4j-jdk14 | 1.5.6 | pom | 1898 |
| org.slf4j | slf4j-parent | 1.5.6 | pom | 7920 |
| org.slf4j | slf4j-parent | 1.6.0 | pom | 9271 |
| org.slf4j | slf4j-parent | 1.7.12 | pom | 11848 |
| org.slf4j | slf4j-parent | 1.7.30 | pom | 13806 |
| org.slf4j | slf4j-parent | 1.7.36 | pom | 14093 |
| org.sonatype.aether | aether-api | 1.7 | jar | 74176 |
| org.sonatype.aether | aether-api | 1.7 | pom | 1673 |
| org.sonatype.aether | aether-impl | 1.7 | jar | 106275 |
| org.sonatype.aether | aether-impl | 1.7 | pom | 3693 |
| org.sonatype.aether | aether-parent | 1.7 | pom | 7725 |
| org.sonatype.aether | aether-spi | 1.7 | jar | 13549 |
| org.sonatype.aether | aether-spi | 1.7 | pom | 1741 |
| org.sonatype.aether | aether-util | 1.7 | jar | 107949 |
| org.sonatype.aether | aether-util | 1.7 | pom | 2062 |
| org.sonatype.forge | forge-parent | 10 | pom | 13560 |
| org.sonatype.forge | forge-parent | 4 | pom | 8397 |
| org.sonatype.forge | forge-parent | 5 | pom | 8363 |
| org.sonatype.forge | forge-parent | 6 | pom | 10754 |
| org.sonatype.oss | oss-parent | 5 | pom | 4082 |
| org.sonatype.oss | oss-parent | 7 | pom | 4824 |
| org.sonatype.oss | oss-parent | 9 | pom | 6569 |
| org.sonatype.plexus | plexus-build-api | 0.0.7 | jar | 8482 |
| org.sonatype.plexus | plexus-build-api | 0.0.7 | pom | 3204 |
| org.sonatype.plexus | plexus-cipher | 1.4 | jar | 13494 |
| org.sonatype.plexus | plexus-cipher | 1.4 | pom | 2065 |
| org.sonatype.plexus | plexus-sec-dispatcher | 1.3 | jar | 28555 |
| org.sonatype.plexus | plexus-sec-dispatcher | 1.3 | pom | 2961 |
| org.sonatype.sisu.inject | guice-bean | 1.4.2 | pom | 2606 |
| org.sonatype.sisu.inject | guice-parent | 3.1.0 | pom | 10926 |
| org.sonatype.sisu.inject | guice-plexus | 1.4.2 | pom | 3128 |
| org.sonatype.sisu | sisu-guice | 2.1.7 | jar | 471737 |
| org.sonatype.sisu | sisu-guice | 2.1.7 | pom | 11061 |
| org.sonatype.sisu | sisu-guice | 3.1.0 | pom | 10149 |
| org.sonatype.sisu | sisu-inject-bean | 1.4.2 | jar | 152989 |
| org.sonatype.sisu | sisu-inject-bean | 1.4.2 | pom | 5457 |
| org.sonatype.sisu | sisu-inject-plexus | 1.4.2 | jar | 201716 |
| org.sonatype.sisu | sisu-inject-plexus | 1.4.2 | pom | 5369 |
| org.sonatype.sisu | sisu-inject | 1.4.2 | pom | 1249 |
| org.sonatype.sisu | sisu-parent | 1.4.2 | pom | 7777 |
| org.sonatype.spice | spice-parent | 12 | pom | 6798 |
| org.sonatype.spice | spice-parent | 15 | pom | 8358 |
| org.sonatype.spice | spice-parent | 16 | pom | 8358 |
| org.sonatype.spice | spice-parent | 17 | pom | 6759 |
| org.springframework | spring-framework-bom | 5.3.6 | pom | 5632 |
| org.testcontainers | testcontainers-bom | 1.15.2 | pom | 6907 |
| oro | oro | 2.0.8 | jar | 65261 |
| oro | oro | 2.0.8 | pom | 140 |
| uk.org.lidalia | sysout-over-slf4j | 1.0.2 | jar | 44143 |
| uk.org.lidalia | sysout-over-slf4j | 1.0.2 | pom | 7420 |
| xml-apis | xml-apis | 1.0.b2 | pom | 2249 |

</details>
