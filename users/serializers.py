from rest_framework import serializers
from .models import User, Service, Category, SpecialistProfile, Schedule, Booking, Review


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('username', 'password', 'email', 'phone', 'role')

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email'),
            phone=validated_data.get('phone'),
            role=validated_data.get('role'),
            password=validated_data['password']
        )
        if user.role == 'specialist':
            SpecialistProfile.objects.create(user=user)
        return user


class UserShortSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'first_name', 'last_name', 'email', 'phone', 'role')
        read_only_fields = fields


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'


class SpecialistProfileSerializer(serializers.ModelSerializer):
    user = UserShortSerializer(read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    reviews_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SpecialistProfile
        fields = (
            'id', 'user', 'bio', 'specialization', 'categories',
            'experience_years', 'city', 'address', 'avatar',
            'is_available', 'average_rating', 'reviews_count',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class ServiceSerializer(serializers.ModelSerializer):
    specialist_name = serializers.CharField(source='specialist.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = Service
        fields = '__all__'
        read_only_fields = ['specialist']


class ScheduleSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source='get_weekday_display', read_only=True)

    class Meta:
        model = Schedule
        fields = ('id', 'specialist', 'weekday', 'weekday_display', 'start_time', 'end_time', 'is_active')
        read_only_fields = ['specialist']


class ReviewSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source='client.username', read_only=True)

    class Meta:
        model = Review
        fields = ('id', 'client', 'specialist', 'booking', 'rating', 'text', 'client_name', 'created_at')
        read_only_fields = ['client', 'specialist']


class BookingSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source='client.username', read_only=True)
    specialist_name = serializers.CharField(source='specialist.username', read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True)

    class Meta:
        model = Booking
        fields = (
            'id', 'client', 'specialist', 'service',
            'client_name', 'specialist_name', 'service_name',
            'date', 'start_time', 'end_time', 'status',
            'comment', 'created_at', 'updated_at',
        )
        read_only_fields = ['client', 'status', 'created_at', 'updated_at']
