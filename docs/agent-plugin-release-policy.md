# Agent Plugin 릴리스 운영 정책 (이슈 #247)

`docs/agent-plugin-packaging.md` 는 패키지 표준과 빌드 산출물의 구조를 다룬다.
이 문서는 그 산출물을 실제로 릴리스로 공표하고 수령자가 검증하는 **표준 외
운영 정책**을 다룬다 — 이슈 #137 §보안·운영 경계가 명시한 "checksum/signature
정책은 별도 문서화" 항목의 이행이다.

## 신뢰 모델: 무결성이지 진본성이 아니다

릴리스 세트의 체크섬(`localcrab-plugin.SHA256SUMS`, `localcrab-plugin-<v>.
RELEASE.SHA256SUMS`)과 아카이브 내부 대사는 다음을 **검출**한다.

- 우발적 손상 — 전송 오류, 부분 다운로드, 디스크 오염.
- 릴리스 세트 내부 불일치 — 아카이브와 사이드카가 서로 다른 소스에서 나왔거나,
  세트 구성 파일이 누락·추가된 경우.

이 체크섬이 **보장하지 않는 것**은 진본성이다. 공격자가 아카이브·패키지
사이드카·`RELEASE.SHA256SUMS` 세 파일을 전부 재계산해 통째로 바꿔치기하면,
로컬 검증(`sha256sum -c`, `--verify`)은 그 위조 세트에 대해서도 통과한다.
체크섬은 자기 자신과의 일관성만 증명하며, 그 세트가 실제로 프로젝트가 만든
것이라는 사실은 별도 경로로만 확인할 수 있다. 이 경계는 코드 쪽에도
성격규정(characterization) 테스트로 남는다 — 전량 재계산 세트가 로컬 검증을
통과함을 실증하는 테스트이지, 버그가 아니다.

서명(GPG/sigstore)은 이 진본성 격차를 메우는 표준적인 방법이지만 이슈 #137
비범위이며 아래 [서명 비도입](#서명-비도입과-근거)에서 이유를 설명한다.

## 공표 기준값

체크섬만으로 메울 수 없는 진본성 격차는 **공표 채널의 분리**로 좁힌다.

- 릴리스를 공표할 때 GitHub Release notes 본문에 `RELEASE.SHA256SUMS`
  **파일 자체**의 sha256 값을 기재한다(파일 안에 담긴 개별 파일 해시가
  아니라, 그 목록 파일을 통째로 해싱한 값).
- 수령자는 다운로드한 `RELEASE.SHA256SUMS` 를 직접 해싱해 notes 에 적힌
  값과 대사한 뒤에야, 그 파일을 신뢰 앵커 삼아 나머지 세트를 검증한다.

이 절차가 검출하는 것은 **첨부 파일만 바꿔치기된 경우**다 — 수령자가
기준값을 첨부 파일이 아니라 notes 본문이라는 별도 표면에서 읽으므로,
다운로드 경로·미러·첨부 저장소에서 첨부만 교체된 세트는 notes 의 기재값과
어긋나 드러난다.

**한계**: notes 기재값은 "독립 신뢰 앵커"가 아니라 **공표 기준값**이다.
릴리스를 공표한 계정이나 저장소의 게시 권한 자체가 침해되면, 공격자는 notes
텍스트와 첨부 파일을 동시에 바꿔 두 값을 서로 일치시킬 수 있다. 이 절차는
"첨부만 조작"을 막을 뿐 "계정 탈취를 동반한 전면 위조"까지는 막지 못한다.
후자를 막으려면 서명과 별도의 신뢰 루트(예: 서명자 키의 대역 외 배포)가
필요하며, 이는 [서명 비도입](#서명-비도입과-근거)에서 다루는 후속 검토
영역이다.

## 수령자 검증 절차

1. GitHub Release 페이지에서 4개 첨부 파일(`localcrab-plugin-<v>.tar.gz`,
   `localcrab-plugin-<v>.COMPATIBILITY.md`, `localcrab-plugin.SHA256SUMS`,
   `localcrab-plugin-<v>.RELEASE.SHA256SUMS`)과 release notes 를 함께 확인한다.
2. `RELEASE.SHA256SUMS` 를 직접 해싱해 notes 에 기재된 값과 대사한다.

   ```bash
   sha256sum localcrab-plugin-<v>.RELEASE.SHA256SUMS
   ```

3. 일치하면 그 파일을 기준으로 나머지 세트를 검증한다.

   ```bash
   sha256sum -c localcrab-plugin-<v>.RELEASE.SHA256SUMS
   ```

4. 아카이브를 설치에 쓰기 전, 세트 내부 상호 대사(아카이브 멤버 ↔ 패키지
   사이드카)까지 확인하려면 빌더의 검증 모드를 쓴다(저장소 checkout 필요).

   ```bash
   python scripts/build_agent_plugin.py --verify --out <다운로드-디렉터리>
   ```

### 불일치 시 대응

어느 단계에서든 해시가 어긋나면:

1. **설치를 중단한다.** 불일치 세트를 프로비저닝이나 실행에 쓰지 않는다.
2. **재취득한다.** 네트워크 손상이 흔한 원인이므로 다시 내려받아 동일
   불일치가 재현되는지 먼저 확인한다.
3. 재현되면 **이슈로 보고한다.** 어떤 파일이 어떤 값으로 어긋났는지(명령
   출력 원문)와 릴리스 태그를 함께 남긴다.

## 독립 재검증

release notes 와 첨부 파일 자체를 신뢰하지 않고 처음부터 다시 확인하려면,
같은 태그의 clean checkout 에서 재현 빌드한 뒤 해시를 대사한다.

```bash
git clone --branch <tag> --depth 1 <repo-url> /tmp/verify-src
cd /tmp/verify-src
python scripts/build_agent_plugin.py --out dist
sha256sum dist/localcrab-plugin-*.tar.gz dist/localcrab-plugin-*.COMPATIBILITY.md \
  dist/localcrab-plugin.SHA256SUMS dist/localcrab-plugin-*.RELEASE.SHA256SUMS
```

이 값을 다운로드한 릴리스 첨부의 해시와 대사한다. 재현성 성질과 도구체계
경계(POSIX + LF checkout 안에서 바이트 동일, zlib 구현이 다른 환경 간의
gz 바이트 동일은 비주장)는 `docs/agent-plugin-packaging.md` §릴리스 산출물과
검증 참고.

## 서명 비도입과 근거

이 정책은 GPG 서명이나 sigstore 같은 서명 인프라를 도입하지 않는다.

- 이슈 #137 의 범위는 discovery·설정 패키징이며, 서명 키 관리·배포·검증
  체계는 별도의 운영 인프라(키 생성·보관·순환, 검증 도구 배포)를 요구해
  범위를 크게 넘어선다.
- 위 [공표 기준값](#공표-기준값) 절차가 "첨부만 바꿔치기"라는 흔한 공격
  표면은 서명 없이도 검출한다. 남는 격차(계정·게시 권한 침해를 동반한
  전면 위조)는 서명으로도 그 서명 키 자체가 안전할 때만 막힌다.
- 서명 도입은 후속 검토 항목으로 남긴다. 필요해지는 시점은 배포 채널이
  다수화되거나(예: 서드파티 미러), 진본성 요구 수준이 "공표 계정 신뢰"를
  넘어설 때다.

## CI artifact 는 비권위 후보다

`.github/workflows/agent-plugin.yml` 의 `validate` job 은 매 push/PR 마다
릴리스 세트를 빌드해 `agent-plugin-release-candidate` 라는 이름의 워크플로
아티팩트로 업로드한다. 이 이름 자체가 성격을 명시한다 — **후보(candidate)**
이지 공표된 릴리스가 아니다.

- CI 아티팩트는 GitHub Actions 접근 권한이 있는 누구나 내려받을 수 있고,
  release notes 의 공표 기준값과 대사할 대상이 아니다.
- CI 아티팩트를 설치나 배포에 직접 쓰지 않는다. 아래 [수동 릴리스
  절차](#수동-릴리스-절차)를 거쳐 공표된 GitHub Release 만 권위 있는
  배포 대상이다.
- CI 아티팩트의 용도는 PR 리뷰 시점에 "릴리스 세트가 실제로 빌드되고
  clean clone 재현·`--verify`·`sha256sum -c` 를 통과하는가"를 확인하는
  것으로 한정한다.

## 수동 릴리스 절차

이 이슈는 자동 GitHub Release publish 워크플로를 도입하지 않는다(비범위,
후속 이관 후보). 릴리스는 아래 수동 절차를 따른다.

1. **태그**: 릴리스할 커밋에 버전 태그를 붙인다(pyproject `[project].version`
   과 일치).
2. **clean checkout 빌드**: 그 태그의 clean checkout 에서 빌드한다.

   ```bash
   git clone --branch <tag> --depth 1 <repo-url> /tmp/release-src
   cd /tmp/release-src
   python scripts/build_agent_plugin.py --out dist
   ```

3. **검증**: 빌드 직후 로컬 검증을 통과하는지 확인한다.

   ```bash
   python scripts/build_agent_plugin.py --verify --out dist
   ```

4. **GitHub Release 생성**: 릴리스 세트 4파일(staged 디렉터리 `dist/
   localcrab-plugin/` 제외)을 첨부하고, notes 본문에 `RELEASE.SHA256SUMS`
   파일 자체의 sha256 값을 기재한다.

   ```bash
   sha256sum dist/localcrab-plugin-*.RELEASE.SHA256SUMS
   gh release create <tag> \
     dist/localcrab-plugin-*.tar.gz \
     dist/localcrab-plugin-*.COMPATIBILITY.md \
     dist/localcrab-plugin.SHA256SUMS \
     dist/localcrab-plugin-*.RELEASE.SHA256SUMS \
     --notes "<RELEASE.SHA256SUMS 의 sha256 값을 포함한 본문>"
   ```

관련 문서: [`docs/agent-plugin-packaging.md`](./agent-plugin-packaging.md)
(패키지 표준과 산출물 구조), [`docs/agent-plugin-compatibility.md`](./agent-plugin-compatibility.md)
(클라이언트 호환성 정본).
