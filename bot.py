#!/usr/bin/env python3
"""SRT 빈자리 감시 Discord Bot"""

import asyncio
import os
from datetime import datetime

import discord
from discord import app_commands
from dotenv import load_dotenv

import srt_client

load_dotenv()

# ─── Bot 설정 ──────────────────────────────────────────────────

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# 채널별 감시 태스크 관리
# key: channel_id, value: {"task": asyncio.Task, "params": dict}
monitors = {}


# ─── 헬퍼 ─────────────────────────────────────────────────────

def station_choices():
    """자주 사용되는 역을 autocomplete choices로 반환."""
    common = ["수서", "동탄", "평택지제", "천안아산", "대전", "동대구", "부산",
              "광명", "서울", "오송", "김천(구미)", "울산(통도사)", "경주",
              "광주송정", "목포", "익산", "전주", "순천", "여수EXPO", "포항"]
    return [app_commands.Choice(name=s, value=s) for s in common if s in srt_client.STATIONS]


def build_schedule_embed(parsed, dpt_name, arv_name, date_str):
    """열차 스케줄을 Discord Embed로 생성."""
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = srt_client.WEEKDAYS_KR[dt.weekday()]

    embed = discord.Embed(
        title=f"🚄 {dpt_name} → {arv_name}",
        description=f"📅 {dt.strftime('%Y.%m.%d')} {weekday}",
        color=discord.Color.blue(),
    )

    lines = []
    for t in parsed:
        gnrm_icon = "🟢" if t["gnrm_avail"] else "🔴"
        sprm_icon = "🟢" if t["sprm_avail"] else "🔴"
        line = (
            f"`{t['index']+1:>2}.` **{t['trainNo']}** "
            f"{srt_client.fmt_time(t['dptTm'])}→{srt_client.fmt_time(t['arvTm'])} "
            f"({t['duration']}) "
            f"일반{gnrm_icon} 특실{sprm_icon}"
        )
        lines.append(line)

    # Embed field 길이 제한(1024자) 대응: 여러 필드로 분할
    chunk = []
    chunk_len = 0
    field_num = 1
    for line in lines:
        if chunk_len + len(line) + 1 > 1000:
            embed.add_field(name=f"열차 목록 ({field_num})", value="\n".join(chunk), inline=False)
            chunk = []
            chunk_len = 0
            field_num += 1
        chunk.append(line)
        chunk_len += len(line) + 1

    if chunk:
        name = "열차 목록" if field_num == 1 else f"열차 목록 ({field_num})"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)

    embed.set_footer(text="🟢 예약가능  🔴 매진")
    return embed


def build_alert_embed(train_info, dpt_name, arv_name, date_str):
    """빈자리 알림 Embed 생성."""
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = srt_client.WEEKDAYS_KR[dt.weekday()]

    seats = []
    if train_info.get("gnrm_ok"):
        seats.append("일반실")
    if train_info.get("sprm_ok"):
        seats.append("특실")

    embed = discord.Embed(
        title="🎉 SRT 빈자리 발견!",
        description=(
            f"**{dpt_name} → {arv_name}**\n"
            f"📅 {dt.strftime('%Y.%m.%d')} {weekday}\n\n"
            f"🚄 열차 **{train_info['trainNo']}**\n"
            f"🕐 {srt_client.fmt_time(train_info['dptTm'])} → {srt_client.fmt_time(train_info['arvTm'])}\n"
            f"💺 **{', '.join(seats)}** 예약 가능!"
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="빠르게 SRTplay에서 예매하세요!")
    return embed


# ─── 슬래시 커맨드 ─────────────────────────────────────────────

@tree.command(name="search", description="SRT 열차 스케줄 조회")
@app_commands.describe(
    departure="출발역",
    arrival="도착역",
    date="출발일 (YYYYMMDD, 예: 20260418)",
    passengers="승객 수 (어른,어린이,경로,장애,유아) 기본: 1,0,0,0,0",
)
@app_commands.choices(departure=station_choices(), arrival=station_choices())
async def search_command(
    interaction: discord.Interaction,
    departure: str,
    arrival: str,
    date: str,
    passengers: str = "1,0,0,0,0",
):
    await interaction.response.defer()

    try:
        pax = [int(x) for x in passengers.split(",")]
        if len(pax) != 5:
            await interaction.followup.send("❌ 승객 수는 5개 값이어야 합니다 (예: 1,0,0,0,0)")
            return
    except ValueError:
        await interaction.followup.send("❌ 승객 수 형식이 올바르지 않습니다 (예: 1,0,0,0,0)")
        return

    try:
        loop = asyncio.get_event_loop()
        html_text = await loop.run_in_executor(
            None, srt_client.fetch_schedule, departure, arrival, date, pax
        )
    except Exception as e:
        await interaction.followup.send(f"❌ 조회 실패: {e}")
        return

    trains = srt_client.parse_trains_from_html(html_text)
    parsed = srt_client.parse_train_list(trains)

    if not parsed:
        await interaction.followup.send("⚠️ 열차 정보를 가져올 수 없습니다. 쿠키 설정을 확인해주세요.")
        return

    embed = build_schedule_embed(parsed, departure, arrival, date)
    await interaction.followup.send(embed=embed)


@tree.command(name="monitor", description="SRT 빈자리 감시 시작")
@app_commands.describe(
    departure="출발역",
    arrival="도착역",
    date="출발일 (YYYYMMDD, 예: 20260418)",
    trains="감시할 열차 번호 (쉼표 구분, 예: 1,3,5 / all=전체)",
    interval="조회 간격 초 (기본: 30)",
    passengers="승객 수 (어른,어린이,경로,장애,유아) 기본: 1,0,0,0,0",
)
@app_commands.choices(departure=station_choices(), arrival=station_choices())
async def monitor_command(
    interaction: discord.Interaction,
    departure: str,
    arrival: str,
    date: str,
    trains: str = "all",
    interval: int = 30,
    passengers: str = "1,0,0,0,0",
):
    channel_id = interaction.channel_id

    if channel_id in monitors:
        await interaction.response.send_message("⚠️ 이 채널에서 이미 감시가 진행 중입니다. `/stop`으로 먼저 중지해주세요.")
        return

    await interaction.response.defer()

    try:
        pax = [int(x) for x in passengers.split(",")]
        if len(pax) != 5:
            await interaction.followup.send("❌ 승객 수는 5개 값이어야 합니다")
            return
    except ValueError:
        await interaction.followup.send("❌ 승객 수 형식이 올바르지 않습니다")
        return

    if interval < 10:
        await interaction.followup.send("❌ 조회 간격은 최소 10초 이상이어야 합니다.")
        return

    # 초기 스케줄 조회로 열차 목록 확인
    try:
        loop = asyncio.get_event_loop()
        html_text = await loop.run_in_executor(
            None, srt_client.fetch_schedule, departure, arrival, date, pax
        )
    except Exception as e:
        await interaction.followup.send(f"❌ 조회 실패: {e}")
        return

    raw_trains = srt_client.parse_trains_from_html(html_text)
    parsed = srt_client.parse_train_list(raw_trains)

    if not parsed:
        await interaction.followup.send("⚠️ 열차 정보를 가져올 수 없습니다.")
        return

    # 감시 대상 인덱스 결정
    if trains.lower() == "all":
        watch_indices = list(range(len(parsed)))
    else:
        try:
            watch_indices = [int(x.strip()) - 1 for x in trains.split(",")]
            if not all(0 <= idx < len(parsed) for idx in watch_indices):
                await interaction.followup.send(f"❌ 열차 번호는 1~{len(parsed)} 범위여야 합니다.")
                return
        except ValueError:
            await interaction.followup.send("❌ 열차 번호 형식이 올바르지 않습니다 (예: 1,3,5)")
            return

    # 감시 정보 표시
    watch_trains = [parsed[i] for i in watch_indices]
    watch_desc = ", ".join(
        f"**{t['trainNo']}**({srt_client.fmt_time(t['dptTm'])})" for t in watch_trains
    )

    embed = discord.Embed(
        title="🔍 빈자리 감시 시작",
        description=(
            f"**{departure} → {arrival}** ({date})\n"
            f"감시 열차: {watch_desc}\n"
            f"조회 간격: {interval}초\n\n"
            f"`/stop` 명령으로 중지"
        ),
        color=discord.Color.orange(),
    )
    await interaction.followup.send(embed=embed)

    # 감시 태스크 시작
    task = asyncio.create_task(
        _monitor_task(
            interaction.channel,
            departure, arrival, date,
            watch_indices, parsed,
            interval, pax,
        )
    )
    monitors[channel_id] = {
        "task": task,
        "params": {
            "departure": departure,
            "arrival": arrival,
            "date": date,
            "interval": interval,
        },
    }


async def _monitor_task(channel, dpt_name, arv_name, date_str,
                        watch_indices, parsed, interval, passengers):
    """비동기 감시 루프."""
    watch_train_nos = {parsed[i]["trainNo"] for i in watch_indices}
    watch_dpt_tms = {parsed[i]["dptTm"] for i in watch_indices}
    found_available = set()
    check_count = 0

    try:
        while True:
            await asyncio.sleep(interval)
            check_count += 1

            try:
                loop = asyncio.get_event_loop()
                html_text = await loop.run_in_executor(
                    None, srt_client.fetch_schedule, dpt_name, arv_name, date_str, passengers
                )
            except Exception:
                continue

            trains = srt_client.parse_trains_from_html(html_text)
            if not trains:
                continue

            for item in trains:
                train_no = item.get("trnNo", "").lstrip("0") or ""
                dpt_tm = item.get("dptTm", "")

                if train_no not in watch_train_nos and dpt_tm not in watch_dpt_tms:
                    continue

                gnrm = item.get("gnrmRsvPsbCdNm", "")
                sprm = item.get("sprmRsvPsbCdNm", "")
                gnrm_ok = srt_client.is_seat_available(gnrm)
                sprm_ok = srt_client.is_seat_available(sprm)

                key = f"{train_no}_{dpt_tm}"

                if (gnrm_ok or sprm_ok) and key not in found_available:
                    found_available.add(key)
                    arv_tm = item.get("arvTm", "")
                    alert_info = {
                        "trainNo": train_no,
                        "dptTm": dpt_tm,
                        "arvTm": arv_tm,
                        "gnrm_ok": gnrm_ok,
                        "sprm_ok": sprm_ok,
                    }
                    embed = build_alert_embed(alert_info, dpt_name, arv_name, date_str)
                    await channel.send(embed=embed)

                # 다시 매진되면 알림 리셋
                if not (gnrm_ok or sprm_ok) and key in found_available:
                    found_available.discard(key)

    except asyncio.CancelledError:
        pass
    finally:
        monitors.pop(channel.id, None)


@tree.command(name="stop", description="현재 채널의 빈자리 감시 중지")
async def stop_command(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    if channel_id not in monitors:
        await interaction.response.send_message("⚠️ 이 채널에서 진행 중인 감시가 없습니다.")
        return

    monitors[channel_id]["task"].cancel()
    monitors.pop(channel_id, None)

    await interaction.response.send_message(
        embed=discord.Embed(
            title="✋ 감시 중지",
            description="빈자리 감시가 중지되었습니다.",
            color=discord.Color.red(),
        )
    )


@tree.command(name="status", description="현재 채널의 감시 상태 확인")
async def status_command(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    if channel_id not in monitors:
        await interaction.response.send_message("감시 중인 열차가 없습니다.")
        return

    params = monitors[channel_id]["params"]
    embed = discord.Embed(
        title="📊 감시 상태",
        description=(
            f"**{params['departure']} → {params['arrival']}**\n"
            f"📅 {params['date']}\n"
            f"⏱ {params['interval']}초 간격"
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="stations", description="사용 가능한 역 목록 조회")
async def stations_command(interaction: discord.Interaction):
    station_list = ", ".join(srt_client.STATIONS.keys())
    embed = discord.Embed(
        title="🚉 사용 가능한 역 목록",
        description=station_list,
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed)


# ─── 이벤트 ───────────────────────────────────────────────────

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ {client.user} 로그인 완료!")
    print(f"   서버 수: {len(client.guilds)}")


# ─── 실행 ─────────────────────────────────────────────────────

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ .env 파일에 DISCORD_TOKEN을 설정해주세요.")
        return
    client.run(token)


if __name__ == "__main__":
    main()
